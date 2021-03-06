from datetime import timedelta

from django.conf import settings
from django.db.models import Q, Count
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from celery.decorators import task

from core.plugins import MachineValidationPluginManager, AllocationSourcePluginManager, EnforcementOverrideChoice
from core.query import (
    contains_credential, only_current, only_current_source, source_in_range,
    inactive_versions
)
from core.models.group import Group
from core.models.size import Size, convert_esh_size
from core.models.volume import Volume, convert_esh_volume
from core.models.instance import convert_esh_instance
from core.models.provider import Provider
from core.models.machine import convert_glance_image, ProviderMachine, ProviderMachineMembership
from core.models.machine_request import MachineRequest
from core.models.application import Application, ApplicationMembership
from core.models.allocation_source import AllocationSource
from core.models.application_version import ApplicationVersion

from service.machine import (
    update_db_membership_for_group, update_cloud_membership_for_machine,
    remove_membership
)
from service.monitoring import (
    _cleanup_missing_instances, _get_instance_owner_map,
    _get_identity_from_tenant_name, allocation_source_overage_enforcement_for
)
from service.driver import get_account_driver
from service.cache import get_cached_driver
from service.exceptions import TimeoutError
from rtwo.models.size import OSSize
from rtwo.exceptions import GlanceConflict, GlanceForbidden
from libcloud.common.exceptions import BaseHTTPError

from threepio import celery_logger


def strfdelta(tdelta, fmt=None):
    from string import Formatter
    if not fmt:
        # The standard, most human readable format.
        fmt = "{D} days {H:02} hours {M:02} minutes {S:02} seconds"
    if tdelta == timedelta():
        return "0 minutes"
    formatter = Formatter()
    return_map = {}
    div_by_map = {'D': 86400, 'H': 3600, 'M': 60, 'S': 1}
    keys = map(lambda x: x[1], list(formatter.parse(fmt)))
    remainder = int(tdelta.total_seconds())

    for unit in ('D', 'H', 'M', 'S'):
        if unit in keys and unit in div_by_map.keys():
            return_map[unit], remainder = divmod(remainder, div_by_map[unit])

    return formatter.format(fmt, **return_map)


def strfdate(datetime_o, fmt=None):
    if not fmt:
        # The standard, most human readable format.
        fmt = "%m/%d/%Y %H:%M:%S"
    if not datetime_o:
        datetime_o = timezone.now()

    return datetime_o.strftime(fmt)


def tenant_id_to_name_map(account_driver):
    """
    INPUT: account driver
    Get a list of projects
    OUTPUT: A dictionary with keys of ID and values of name
    """
    all_projects = account_driver.list_projects()
    return {tenant.id: tenant.name for tenant in all_projects}


@task(name="prune_machines")
def prune_machines():
    """
    Query the cloud and remove any machines
    that exist in the DB but can no longer be found.
    """
    for p in Provider.get_active():
        prune_machines_for.apply_async(args=[p.id])


@task(name="prune_machines_for")
def prune_machines_for(
    provider_id,
    print_logs=False,
    dry_run=False,
    forced_removal=False,
    validate=True
):
    """
    Look at the list of machines (as seen by the AccountProvider)
    if a machine cannot be found in the list, remove it.
    NOTE: BEFORE CALLING THIS TASK you should ensure
    that the AccountProvider can see ALL images.
    Failure to do so will result in any image unseen by the admin
    to be prematurely end-dated and removed from the API/UI.
    """
    provider = Provider.objects.get(id=provider_id)
    now = timezone.now()
    if print_logs:
        console_handler = _init_stdout_logging()
    celery_logger.info(
        "Starting prune_machines for Provider %s @ %s" % (provider, now)
    )

    if provider.is_active():
        account_driver = get_account_driver(provider)
        db_machines = ProviderMachine.objects.filter(
            only_current_source(), instance_source__provider=provider
        )
        cloud_machines = account_driver.list_all_images()
    else:
        db_machines = ProviderMachine.objects.filter(
            source_in_range(),    # like 'only_current..' w/o active_provider
            instance_source__provider=provider
        )
        cloud_machines = []

    machine_validator = MachineValidationPluginManager.get_validator(
        account_driver
    )
    cloud_machines = [
        cloud_machine for cloud_machine in cloud_machines
        if not validate or machine_validator.machine_is_valid(cloud_machine)
    ]

    # Don't do anything if cloud machines == [None,[]]
    if not cloud_machines and not forced_removal:
        return

    # Loop 1 - End-date All machines in the DB that
    # can NOT be found in the cloud.
    mach_count = _end_date_missing_database_machines(
        db_machines, cloud_machines, now=now, dry_run=dry_run
    )

    # Loop 2 and 3 - Capture all (still-active) versions without machines,
    # and all applications without versions.
    # These are 'outliers' and mainly here for safety-check purposes.
    ver_count = _remove_versions_without_machines(now=now)
    app_count = _remove_applications_without_versions(now=now)

    # Loop 4 - All 'Application' DB objects require
    # >=1 Version with >=1 ProviderMachine (ACTIVE!)
    # Apps that don't meet this criteria should be end-dated.
    app_count += _update_improperly_enddated_applications(now)

    # Clear out application, provider machine, and version memberships
    # if the result is >128.
    # Additionally, remove all users who are not in the machine request (if one exists).
    _clean_memberships(db_machines, account_driver)

    celery_logger.info(
        "prune_machines completed for Provider %s : "
        "%s Applications, %s versions and %s machines pruned." %
        (provider, app_count, ver_count, mach_count)
    )
    if print_logs:
        _exit_stdout_logging(console_handler)


@task(name="monitor_machines")
def monitor_machines():
    """
    Update machines by querying the Cloud for each active provider.
    """
    for p in Provider.get_active():
        monitor_machines_for.apply_async(args=[p.id])


@task(name="monitor_machines_for")
def monitor_machines_for(
    provider_id,
    limit_machines=[],
    print_logs=False,
    dry_run=False,
    validate=True
):
    """
    Run the set of tasks related to monitoring machines for a provider.
    Optionally, provide a list of usernames to monitor
    While debugging, print_logs=True can be very helpful.
    start_date and end_date allow you to search a 'non-standard' window of time.

    NEW LOGIC:
    """
    provider = Provider.objects.get(id=provider_id)

    if print_logs:
        console_handler = _init_stdout_logging()

    account_driver = get_account_driver(provider)
    #Bail out if account driver is invalid
    if not account_driver:
        if print_logs:
            _exit_stdout_logging(console_handler)
        return []

    if account_driver.user_manager.version == 2:
        #Old providers need to use v1 glance to get owner information.
        cloud_machines_dict = account_driver.image_manager.list_v1_images()
        cloud_machines = account_driver.list_all_images()
        account_driver.add_owner_to_machine(cloud_machines, cloud_machines_dict)
    else:
        cloud_machines = account_driver.list_all_images()

    if limit_machines:
        cloud_machines = [
            cm for cm in cloud_machines if cm.id in limit_machines
        ]
    db_machines = []
    # ASSERT: All non-end-dated machines in the DB can be found in the cloud
    # if you do not believe this is the case, you should call 'prune_machines_for'
    machine_validator = MachineValidationPluginManager.get_validator(
        account_driver
    )
    for cloud_machine in cloud_machines:
        if validate and not machine_validator.machine_is_valid(cloud_machine):
            continue
        owner = cloud_machine.get('owner')
        if owner:
            owner_project = account_driver.get_project_by_id(owner)
        else:
            owner = cloud_machine.get('application_owner')
            owner_project = account_driver.get_project(owner)
        #STEP 1: Get the application, version, and provider_machine registered in Atmosphere
        (db_machine, created) = convert_glance_image(
            account_driver, cloud_machine, provider.uuid, owner_project
        )
        if not db_machine:
            continue
        db_machines.append(db_machine)
        #STEP 2: For any private cloud_machine, convert the 'shared users' as known by cloud
        #        into DB relationships: ApplicationVersionMembership, ProviderMachineMembership
        update_image_membership(account_driver, cloud_machine, db_machine)

        # STEP 3: if ENFORCING -- occasionally 're-distribute' any ACLs that
        # are *listed on DB but not on cloud* -- removals should be done
        # explicitly, outside of this function
        if settings.ENFORCING:
            distribute_image_membership(account_driver, cloud_machine, provider)

        # ASSERTIONS about this method:
        # 1) We will never 'remove' membership,
        # 2) We will never 'remove' a public or private flag as listed in application.
        # 2b) Future: Individual versions/machines as described by relationships above dictate whats shown in the application.

    if print_logs:
        _exit_stdout_logging(console_handler)
    return db_machines


def distribute_image_membership(account_driver, cloud_machine, provider):
    """
    Based on what we know about the DB, at a minimum, ensure that their projects are added to the image_members list for this cloud_machine.
    """
    pm = ProviderMachine.objects.get(
        instance_source__provider=provider,
        instance_source__identifier=cloud_machine.id
    )
    group_ids = ProviderMachineMembership.objects.filter(provider_machine=pm
                                                        ).values_list(
                                                            'group', flat=True
                                                        )
    groups = Group.objects.filter(id__in=group_ids)
    for group in groups:
        try:
            celery_logger.info(
                "Add %s to cloud membership for %s" % (group, pm)
            )
            update_cloud_membership_for_machine(pm, group)
        except TimeoutError:
            celery_logger.warn(
                "Failed to add cloud membership for %s - Operation timed out" %
                group
            )
    return groups


def _get_all_access_list(account_driver, db_machine, cloud_machine):
    """
    Input: AccountDriver, ProviderMachine, glance_image
    Output: A list of _all project names_ that should be included on `cloud_machine`

    This list will include:
    - Users who match the provider_machine's application.access_list
    - Users who are already approved to use the `cloud_machine`
    - The owner of the application/Creator of the MachineRequest
    - If using settings.REPLICATION_PROVIDER:
      - include all those approved on the replication provider's copy of the image
    """
    #TODO: In a future update to 'imaging' we might image 'as the user' rather than 'as the admin user', in this case we should just use 'owner' metadata

    image_owner = cloud_machine.get('application_owner')
    # NOTE: This assumes that the 'owner' (atmosphere user) == 'project_name' (Openstack)
    # Always include the original application owner
    owner_set = set()
    if image_owner:
        owner_set.add(image_owner)

    if hasattr(cloud_machine, 'id'):
        image_id = cloud_machine.id
    elif type(cloud_machine) == dict:
        image_id = cloud_machine.get('id')
    else:
        raise ValueError("Unexpected cloud_machine: %s" % cloud_machine)

    existing_members = account_driver.get_image_members(image_id, None)
    # Extend to include based on projects already granted access to the image
    cloud_shared_set = {p.name for p in existing_members}

    has_machine_request = MachineRequest.objects.filter(
        new_machine__instance_source__identifier=cloud_machine.id,
        status__name='completed'
    ).last()
    machine_request_set = set()
    if has_machine_request:
        access_list = has_machine_request.get_access_list()
        # NOTE: This assumes that every name in
        #      accesslist (AtmosphereUser) == project_name(Openstack)
        machine_request_set = {name.strip() for name in access_list}

    # Extend to include new names found by application pattern_match
    parent_app = db_machine.application_version.application
    access_list_set = set(
        parent_app.get_users_from_access_list().values_list(
            'username', flat=True
        )
    )
    shared_project_names = list(
        owner_set | cloud_shared_set | machine_request_set | access_list_set
    )
    return shared_project_names


def update_image_membership(account_driver, cloud_machine, db_machine):
    """
    Given a cloud_machine and db_machine, create any relationships possible for ProviderMachineMembership and ApplicationVersionMembership
    Return a list of all group names who have been given share access.
    """
    image_visibility = cloud_machine.get('visibility', 'private')
    if image_visibility.lower() == 'public':
        return
    shared_project_names = _get_all_access_list(
        account_driver, db_machine, cloud_machine
    )

    #Future-FIXME: This logic expects project_name == Group.name
    #       When this changes, logic should update to include checks for:
    #       - Lookup Identities with this project_name
    #       - Share with group that has IdentityMembership
    #       - Alternatively, consider changing ProviderMachineMembership
    #       to point to Identity for a 1-to-1 mapping.
    groups = Group.objects.filter(name__in=shared_project_names)

    # THIS IS A HACK - some images have been 'compromised' in this event,
    # reset the access list _back_ to the last-known-good configuration, based
    # on a machine request.
    has_machine_request = MachineRequest.objects.filter(
        new_machine__instance_source__identifier=cloud_machine.id,
        status__name='completed'
    ).last()
    parent_app = db_machine.application_version.application
    if len(shared_project_names) > 128:
        celery_logger.warn(
            "Application %s has too many shared users. Consider running 'prune_machines' to cleanup",
            parent_app
        )
        if not has_machine_request:
            return
        access_list = has_machine_request.get_access_list()
        shared_project_names = access_list
    #ENDHACK
    for group in groups:
        update_db_membership_for_group(db_machine, group)
    return groups


def remove_machine(db_machine, now_time=None, dry_run=False):
    """
    End date the DB ProviderMachine
    If all PMs are end-dated, End date the ApplicationVersion
    if all Versions are end-dated, End date the Application
    """
    if not now_time:
        now_time = timezone.now()

    db_machine.end_date = now_time
    celery_logger.info("End dating machine: %s" % db_machine)
    if not dry_run:
        db_machine.save()

    db_version = db_machine.application_version
    if db_version.machines.filter(
    # Look and see if all machines are end-dated.
        Q(instance_source__end_date__isnull=True) |
        Q(instance_source__end_date__gt=now_time)
    ).count() != 0:
        # Other machines exist.. No cascade necessary.
        return True
    # Version also completely end-dated. End date this version.
    db_version.end_date = now_time
    celery_logger.info("End dating version: %s" % db_version)
    if not dry_run:
        db_version.save()

    db_application = db_version.application
    if db_application.versions.filter(
    # If all versions are end-dated
        only_current(now_time)
    ).count() != 0:
        # Other versions exist.. No cascade necessary..
        return True
    db_application.end_date = now_time
    celery_logger.info("End dating application: %s" % db_application)
    if not dry_run:
        db_application.save()
    return True


def memoized_image(account_driver, db_machine, image_maps={}):
    provider = db_machine.instance_source.provider
    identifier = db_machine.instance_source.identifier
    cloud_machine = image_maps.get((provider, identifier))
    # Return memoized result
    if cloud_machine:
        return cloud_machine
    # Retrieve and remember
    cloud_machine = account_driver.get_image(identifier)
    image_maps[(provider, identifier)] = cloud_machine
    return cloud_machine


def memoized_driver(machine, account_drivers={}):
    provider = machine.instance_source.provider
    account_driver = account_drivers.get(provider)
    if not account_driver:
        account_driver = get_account_driver(provider)
        if not account_driver:
            raise Exception(
                "Cannot instantiate an account driver for %s" % provider
            )
        account_drivers[provider] = account_driver
    return account_driver


def memoized_tenant_name_map(account_driver, tenant_list_maps={}):
    tenant_id_name_map = tenant_list_maps.get(account_driver.core_provider)
    if not tenant_id_name_map:
        tenant_id_name_map = tenant_id_to_name_map(account_driver)
        tenant_list_maps[account_driver.core_provider] = tenant_id_name_map

    return tenant_id_name_map


def get_current_members(account_driver, machine, tenant_id_name_map):
    current_membership = account_driver.image_manager.shared_images_for(
        image_id=machine.identifier
    )

    current_tenants = []
    for membership in current_membership:
        tenant_id = membership.member_id
        tenant_name = tenant_id_name_map.get(tenant_id)
        if tenant_name:
            current_tenants.append(tenant_name)
    return current_tenants


def add_application_membership(application, identity, dry_run=False):
    for membership_obj in identity.identity_memberships.all():
        # For every 'member' of this identity:
        group = membership_obj.member
        # Add an application membership if not already there
        if application.applicationmembership_set.filter(group=group
                                                       ).count() == 0:
            celery_logger.info(
                "Added ApplicationMembership %s for %s" %
                (group.name, application.name)
            )
            if not dry_run:
                ApplicationMembership.objects.create(
                    application=application, group=group
                )
        else:
            #celery_logger.debug("SKIPPED _ Group %s already ApplicationMember for %s" % (group.name, application.name))
            pass


@task(name="monitor_resources")
def monitor_resources():
    """
    Update instances for each active provider.
    """
    for p in Provider.get_active():
        monitor_resources_for.apply_async(args=[p.id])


@task(name="monitor_resources_for")
def monitor_resources_for(provider_id, users=None, print_logs=False):
    """
    Run the set of tasks related to monitoring all cloud resources for a provider.
    """
    resources = {}
    sizes = monitor_sizes_for(provider_id, print_logs=print_logs)
    volumes = monitor_volumes_for(provider_id, print_logs=print_logs)
    machines = monitor_machines_for(provider_id, print_logs=print_logs)
    instances = monitor_instances_for(
        provider_id, users=users, print_logs=print_logs
    )
    resources.update(
        {
            'instances': instances,
            'machines': machines,
            'sizes': sizes,
            'volumes': volumes,
        }
    )
    return resources


@task(name="monitor_instances")
def monitor_instances():
    """
    Update instances for each active provider.
    """
    for p in Provider.get_active():
        monitor_instances_for.apply_async(args=[p.id])


@task(name="monitor_allocation_sources")
def monitor_allocation_sources(usernames=()):
    """
    Monitor allocation sources, if a snapshot shows that all compute has been used, then enforce as necessary
    """
    celery_logger.debug('monitor_allocation_sources - usernames: %s', usernames)
    allocation_sources = AllocationSource.objects.all()
    for allocation_source in allocation_sources.order_by('name'):
        celery_logger.debug(
            'monitor_allocation_sources - allocation_source: %s',
            allocation_source
        )
        for user in allocation_source.all_users.order_by('username'):
            celery_logger.debug('monitor_allocation_sources - user: %s', user)
            if usernames and user.username not in usernames:
                celery_logger.info(
                    "Skipping User %s - not in the list" % user.username
                )
                continue
            over_allocation = allocation_source.is_over_allocation(user)
            celery_logger.debug(
                'monitor_allocation_sources - user: %s, over_allocation: %s',
                user, over_allocation
            )

            enforcement_override_choice = AllocationSourcePluginManager.get_enforcement_override(
                user, allocation_source
            )
            celery_logger.debug(
                'monitor_allocation_sources - enforcement_override_choice: %s',
                enforcement_override_choice
            )

            if over_allocation and enforcement_override_choice == EnforcementOverrideChoice.NEVER_ENFORCE:
                celery_logger.debug(
                    'Allocation source is over allocation, but %s + user %s has an override of %s, '
                    'therefore not enforcing', allocation_source, user,
                    enforcement_override_choice
                )
                continue

            if not over_allocation and enforcement_override_choice == EnforcementOverrideChoice.ALWAYS_ENFORCE:
                celery_logger.debug(
                    'Allocation source is not over allocation, but %s + user %s has an override of %s, '
                    'therefore enforcing', allocation_source, user,
                    enforcement_override_choice
                )
                # Note: The enforcing happens in the next `if` statement.
            if over_allocation or enforcement_override_choice == EnforcementOverrideChoice.ALWAYS_ENFORCE:
                assert enforcement_override_choice in (
                    EnforcementOverrideChoice.NO_OVERRIDE,
                    EnforcementOverrideChoice.ALWAYS_ENFORCE
                )
                celery_logger.debug(
                    'monitor_allocation_sources - Going to enforce on user: %s',
                    user
                )
                allocation_source_overage_enforcement_for_user.apply_async(
                    args=(allocation_source, user)
                )


@task(name="allocation_source_overage_enforcement_for_user")
def allocation_source_overage_enforcement_for_user(allocation_source, user):
    celery_logger.debug(
        'allocation_source_overage_enforcement_for_user - allocation_source: %s, user: %s',
        allocation_source, user
    )
    user_instances = []
    for identity in user.current_identities:
        try:
            celery_logger.debug(
                'allocation_source_overage_enforcement_for_user - identity: %s',
                identity
            )
            affected_instances = allocation_source_overage_enforcement_for(
                allocation_source, user, identity
            )
            user_instances.extend(affected_instances)
        except Exception:
            celery_logger.exception(
                'allocation_source_overage_enforcement_for allocation_source: %s, user: %s, and identity: %s',
                allocation_source, user, identity
            )
    return user_instances


@task(name="monitor_instances_for")
def monitor_instances_for(
    provider_id, users=None, print_logs=False, start_date=None, end_date=None
):
    """
    Run the set of tasks related to monitoring instances for a provider.
    Optionally, provide a list of usernames to monitor
    While debugging, print_logs=True can be very helpful.
    start_date and end_date allow you to search a 'non-standard' window of time.
    """
    provider = Provider.objects.get(id=provider_id)

    # For now, lets just ignore everything that isn't openstack.
    if 'openstack' not in provider.type.name.lower():
        return
    instance_map = _get_instance_owner_map(provider, users=users)

    if print_logs:
        console_handler = _init_stdout_logging()
    seen_instances = []
    # DEVNOTE: Potential slowdown running multiple functions
    # Break this out when instance-caching is enabled
    if not settings.ENFORCING:
        celery_logger.debug('Settings dictate allocations are NOT enforced')
    for tenant_name in sorted(instance_map.keys()):
        running_instances = instance_map[tenant_name]
        identity = _get_identity_from_tenant_name(provider, tenant_name)
        if identity and running_instances:
            try:
                driver = get_cached_driver(identity=identity)
                core_running_instances = [
                    convert_esh_instance(
                        driver, inst, identity.provider.uuid, identity.uuid,
                        identity.created_by
                    ) for inst in running_instances
                ]
                seen_instances.extend(core_running_instances)
            except Exception:
                celery_logger.exception(
                    "Could not convert running instances for %s" % tenant_name
                )
                continue
        else:
            # No running instances.
            core_running_instances = []
        # Using the 'known' list of running instances, cleanup the DB
        _cleanup_missing_instances(identity, core_running_instances)
    if print_logs:
        _exit_stdout_logging(console_handler)
    # return seen_instances  NOTE: this has been commented out to avoid PicklingError!
    # TODO: Uncomment the above, Determine what _we can return_ and return that instead....
    return


@task(name="monitor_volumes")
def monitor_volumes():
    """
    Update volumes for each active provider.
    """
    for p in Provider.get_active():
        monitor_volumes_for.apply_async(args=[p.id])


@task(name="monitor_volumes_for")
def monitor_volumes_for(provider_id, print_logs=False):
    """
    Run the set of tasks related to monitoring sizes for a provider.
    Optionally, provide a list of usernames to monitor
    While debugging, print_logs=True can be very helpful.
    start_date and end_date allow you to search a 'non-standard' window of time.
    """
    from service.driver import get_account_driver
    from core.models import Identity
    if print_logs:
        console_handler = _init_stdout_logging()

    provider = Provider.objects.get(id=provider_id)
    account_driver = get_account_driver(provider)
    # Non-End dated volumes on this provider
    db_volumes = Volume.objects.filter(
        only_current_source(), instance_source__provider=provider
    )
    all_volumes = account_driver.admin_driver.list_all_volumes(timeout=30)
    seen_volumes = []
    for cloud_volume in all_volumes:
        try:
            core_volume = convert_esh_volume(
                cloud_volume, provider_uuid=provider.uuid
            )
            seen_volumes.append(core_volume)
        except ObjectDoesNotExist:
            tenant_id = cloud_volume.extra['object'][
                'os-vol-tenant-attr:tenant_id']
            tenant = account_driver.get_project_by_id(tenant_id)
            tenant_name = tenant.name if tenant else tenant_id
            try:
                if not tenant:
                    celery_logger.warn(
                        "Warning: tenant_id %s found on volume %s, "
                        "but did not exist from the account driver "
                        "perspective.", tenant_id, cloud_volume
                    )
                    raise ObjectDoesNotExist()
                identity = Identity.objects.filter(
                    contains_credential('ex_project_name', tenant_name),
                    provider=provider
                ).first()
                if not identity:
                    raise ObjectDoesNotExist()
                core_volume = convert_esh_volume(
                    cloud_volume, provider.uuid, identity.uuid,
                    identity.created_by
                )
            except ObjectDoesNotExist:
                celery_logger.info(
                    "Skipping Volume %s - No Identity for: Provider:%s + Project Name:%s"
                    % (cloud_volume.id, provider, tenant_name)
                )
            pass

    now_time = timezone.now()
    needs_end_date = [
        volume for volume in db_volumes if volume not in seen_volumes
    ]
    for volume in needs_end_date:
        celery_logger.debug("End dating inactive volume: %s" % volume)
        volume.end_date = now_time
        volume.save()

    if print_logs:
        _exit_stdout_logging(console_handler)
    for vol in seen_volumes:
        vol.esh = None
    return [vol.instance_source.identifier for vol in seen_volumes]


@task(name="monitor_sizes")
def monitor_sizes():
    """
    Update sizes for each active provider.
    """
    for p in Provider.get_active():
        monitor_sizes_for.apply_async(args=[p.id])


@task(name="monitor_sizes_for")
def monitor_sizes_for(provider_id, print_logs=False):
    """
    Run the set of tasks related to monitoring sizes for a provider.
    Optionally, provide a list of usernames to monitor
    While debugging, print_logs=True can be very helpful.
    start_date and end_date allow you to search a 'non-standard' window of time.
    """
    from service.driver import get_admin_driver

    if print_logs:
        console_handler = _init_stdout_logging()

    provider = Provider.objects.get(id=provider_id)
    admin_driver = get_admin_driver(provider)
    # Non-End dated sizes on this provider
    db_sizes = Size.objects.filter(only_current(), provider=provider)
    all_sizes = admin_driver.list_sizes()
    seen_sizes = []
    for cloud_size in all_sizes:
        core_size = convert_esh_size(cloud_size, provider.uuid)
        seen_sizes.append(core_size)

    now_time = timezone.now()
    needs_end_date = [size for size in db_sizes if size not in seen_sizes]
    for size in needs_end_date:
        celery_logger.debug("End dating inactive size: %s" % size)
        size.end_date = now_time
        size.save()

    # Find home for 'Unknown Size'
    unknown_sizes = Size.objects.filter(
        provider=provider, name__contains='Unknown Size'
    )
    for size in unknown_sizes:
        # Lookup sizes may not show up in 'list_sizes'
        if size.alias == 'N/A':
            continue    # This is a sentinal value added for a separate purpose.
        try:
            libcloud_size = admin_driver.get_size(
                size.alias, forced_lookup=True
            )
        except BaseHTTPError as error:
            if error.code == 404:
                # The size may have been truly deleted
                continue
        if not libcloud_size:
            continue
        cloud_size = OSSize(libcloud_size)
        core_size = convert_esh_size(cloud_size, provider.uuid)

    if print_logs:
        _exit_stdout_logging(console_handler)
    for size in seen_sizes:
        size.esh = None
    return seen_sizes


def _clean_memberships(db_machines, acct_driver=None):
    """
    For each db_machine, check the # of shared access.
    If the # is >128, this application was made in error
    and should be 'cleaned' so it can be re-built in the next
    run of 'monitor_machines'
    """
    for db_machine in db_machines:
        members_qs = db_machine.members.all()
        group_key = 'group__name'
        if members_qs.count() < 128:
            members_qs = db_machine.application_version.membership.all()
        if members_qs.count() < 128:
            members_qs = db_machine.application.applicationmembership_set.all()
            group_key = 'group_ptr__name'
        if members_qs.count() < 128:
            continue
        for member in members_qs.order_by(group_key):
            image_version = db_machine.application_version
            remove_membership(image_version, member.group, acct_driver)


def _end_date_missing_database_machines(
    db_machines, cloud_machines, now=None, dry_run=False
):
    if not now:
        now = timezone.now()
    mach_count = 0
    cloud_machine_ids = [mach.id for mach in cloud_machines]
    for machine in db_machines:
        cloud_match = [
            mach for mach in cloud_machine_ids if mach == machine.identifier
        ]
        if not cloud_match:
            remove_machine(machine, now, dry_run=dry_run)
            mach_count += 1
    return mach_count


def _remove_versions_without_machines(now=None):
    if not now:
        now = timezone.now()
    ver_count = 0
    versions_without_machines = ApplicationVersion.objects.filter(
        machines__isnull=True, end_date__isnull=True
    )
    ver_count = _perform_end_date(versions_without_machines, now)
    return ver_count


def _remove_applications_without_versions(now=None):
    if not now:
        now = timezone.now()
    app_count = 0
    apps_without_versions = Application.objects.filter(
        versions__isnull=True, end_date__isnull=True
    )
    app_count = _perform_end_date(apps_without_versions, now)
    return app_count


def _update_improperly_enddated_applications(now=None):
    if not now:
        now = timezone.now()
    improperly_enddated_apps = Application.objects.annotate(
        num_versions=Count('versions'),
        num_machines=Count('versions__machines')
    ).filter(
        inactive_versions(),
    # AND application has already been end-dated.
        end_date__isnull=False
    )
    app_count = _perform_end_date(improperly_enddated_apps, now)
    return app_count


def _perform_end_date(queryset, end_dated_at):
    count = 0
    for model in queryset:
        model.end_date_all(end_dated_at)
        count += 1
    return count


def _share_image(
    account_driver, cloud_machine, identity, members, dry_run=False
):
    """
    INPUT: use account_driver to share cloud_machine with identity (if not in 'members' list)
    """
    # Skip tenant-names who are NOT in the DB, and tenants who are already included
    missing_tenant = identity.credential_set.filter(
        ~Q(value__in=members), key='ex_tenant_name'
    )
    if missing_tenant.count() == 0:
        #celery_logger.debug("SKIPPED _ Image %s already shared with %s" % (cloud_machine.id, identity))
        return
    elif missing_tenant.count() > 1:
        raise Exception("Safety Check -- You should not be here")
    tenant_name = missing_tenant[0]
    cloud_machine_is_public = cloud_machine.is_public if hasattr(
        cloud_machine, 'is_public'
    ) else cloud_machine.get('visibility', '') == 'public'
    if cloud_machine_is_public:
        celery_logger.info("Making Machine %s private" % cloud_machine.id)
        if not dry_run:
            account_driver.image_manager.glance.images.update(
                cloud_machine.id, visibility='shared'
            )

    celery_logger.info(
        "Sharing image %s<%s>: %s with %s" % (
            cloud_machine.id, cloud_machine.name, identity.provider.location,
            tenant_name.value
        )
    )
    if not dry_run:
        try:
            account_driver.image_manager.share_image(
                cloud_machine, tenant_name.value
            )
        except GlanceConflict as exc:
            if 'already associated with image' in exc.message:
                pass
        except GlanceForbidden as exc:
            if 'Public images do not have members' in exc.message:
                celery_logger.warn(
                    "CONFLICT -- This image should have been marked 'shared'! %s"
                    % cloud_machine
                )
                pass
    return


def _exit_stdout_logging(consolehandler):
    if settings.DEBUG:
        return
    celery_logger.removeHandler(consolehandler)


def _init_stdout_logging(logger=None):
    if settings.DEBUG:
        return
    if not logger:
        logger = celery_logger
    import logging
    import sys
    consolehandler = logging.StreamHandler(sys.stdout)
    consolehandler.setLevel(logging.DEBUG)
    logger.addHandler(consolehandler)
    return consolehandler
