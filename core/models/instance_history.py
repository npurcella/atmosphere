"""
  Instance status history model for atmosphere.
"""
from uuid import uuid4
from datetime import timedelta

from django.db import models, transaction, DatabaseError
from django.db.models import ObjectDoesNotExist
from django.contrib.postgres.fields import JSONField

from django.utils import timezone

from atmo_logger import logger


class InstanceStatus(models.Model):
    """
    Used to enumerate the types of actions
    (I.e. Stopped, Suspended, Active, Deleted)

    FIXME: (Idea) -- adding a new field, is_final_state
    Example of 'is_final_state' status for Openstack:
        - active
          suspended
          shutoff
          error
          deleted
          unknown
    Example of 'is_final_state = False' status for Openstack:
        - networking
          deploying
          deploy_error

    """
    name = models.CharField(max_length=128)

    def __unicode__(self):
        return "%s" % self.name

    class Meta:
        db_table = "instance_status"
        app_label = "core"


class InstanceStatusHistory(models.Model):
    """
    Used to keep track of each change in instance status
    (Useful for time management)

    #FIXME: we might want to handle `InstanceStatus` + `activity` in a different way.
    """
    uuid = models.UUIDField(default=uuid4, unique=True, editable=False)
    instance = models.ForeignKey("Instance")
    size = models.ForeignKey("Size")
    status = models.ForeignKey(InstanceStatus)
    activity = models.CharField(max_length=36, null=True, blank=True)
    start_date = models.DateTimeField(default=timezone.now)
    end_date = models.DateTimeField(null=True, blank=True)
    extra = JSONField(null=True, blank=True)

    def previous(self):
        """
        Given that you are a node on a linked-list, traverse yourself backwards
        """
        if self.instance.start_date == self.start_date:
            raise LookupError(
                "This is the first state of instance %s" % self.instance
            )
        try:
            history = self.instance.instancestatushistory_set.get(
                start_date=self.end_date
            )
            if history.id == self.id:
                raise ValueError(
                    "There was no matching transaction for Instance:%s end-date:%s"
                    % (self.instance, self.end_date)
                )
        except ObjectDoesNotExist:
            raise ValueError(
                "There was no matching transaction for Instance:%s end-date:%s"
                % (self.instance, self.end_date)
            )

    def next(self):
        """
        Given that you are a node on a linked-list, traverse yourself forwards
        """
        # In this situation, the instance is presumably still running.
        if not self.end_date:
            if self.instance.end_date:
                raise ValueError(
                    "Whoa! The instance %s has been terminated, but status %s has not! This could leak time"
                    % (self.instance, self)
                )
            raise LookupError(
                "This is the final state of instance %s" % self.instance
            )
        # In this situation, the end_date of the final history is an exact match to the instance's end-date.
        if self.instance.end_date == self.end_date:
            raise LookupError(
                "This is the final state of instance %s" % self.instance
            )
        # In this situation, the end_date of the final history is "a little off" from the instance's end-date.
        if self == self.instance.get_last_history():
            raise LookupError(
                "This is the final state of instance %s" % self.instance
            )
        try:
            return self.instance.instancestatushistory_set.get(
                start_date=self.end_date
            )
        except ObjectDoesNotExist:
            raise ValueError(
                "There was no matching transaction for Instance:%s end-date:%s"
                % (self.instance, self.end_date)
            )

    @classmethod
    def transaction(
        cls,
        status_name,
        activity,
        instance,
        size,
        extra=None,
        start_time=None,
        last_history=None
    ):
        try:
            with transaction.atomic():
                if not last_history:
                    last_history = instance.get_last_history()
                    if not last_history:
                        raise ValueError(
                            "A previous history is required "
                            "to perform a transaction. Instance:%s" %
                            (instance, )
                        )
                    elif last_history.end_date:
                        raise ValueError(
                            "Old history already has end date: %s" %
                            last_history
                        )
                last_history.end_date = start_time
                last_history.save()
                new_history = InstanceStatusHistory.create_history(
                    status_name,
                    instance,
                    size,
                    start_date=start_time,
                    activity=activity,
                    extra=extra
                )
                logger.info(
                    "Status Update - User:%s Instance:%s "
                    "Old:%s New:%s Time:%s" % (
                        instance.created_by, instance.provider_alias,
                        last_history.status.name, new_history.status.name,
                        new_history.start_date
                    )
                )
                new_history.save()
            return new_history
        except DatabaseError:
            logger.exception(
                "instance_status_history: Lock is already acquired by"
                "another transaction."
            )

    @staticmethod
    def _build_extra(
        status_name=None,
        fault=None,
        deploy_fault_message=None,
        deploy_fault_trace=None
    ):
        extra = {}
        # Only compute this for deploy_error or user_deploy_error (seen as active)
        if status_name not in ['active', 'deploy_error']:
            return extra
        if fault:
            if type(fault) == dict:
                extra['display_error'] = fault.get('message')
                extra['traceback'] = fault.get('details')
            else:
                logger.warn("Invalid 'fault':(%s) expected dict", fault)
        if deploy_fault_message and deploy_fault_trace:
            extra['display_error'] = deploy_fault_message
            extra['traceback'] = deploy_fault_trace
        elif deploy_fault_message or deploy_fault_trace:
            logger.warn(
                "Invalid metadata: Expected 'deploy_fault_message'(%s) "
                "AND 'deploy_fault_trace'(%s), but received only one",
                deploy_fault_message, deploy_fault_trace
            )

        return extra

    @classmethod
    def create_history(
        cls,
        status_name,
        instance,
        size,
        start_date=None,
        end_date=None,
        activity=None,
        extra=None
    ):
        """
        Creates a new (Unsaved!) InstanceStatusHistory
        """
        status, _ = InstanceStatus.objects.get_or_create(name=status_name)
        new_history = InstanceStatusHistory(
            instance=instance,
            size=size,
            status=status,
            activity=activity,
            extra=extra
        )
        if start_date:
            new_history.start_date = start_date
            logger.debug("Created new history object: %s " % (new_history))
        if end_date and not new_history.end_date:
            new_history.end_date = end_date
            logger.debug("End-dated new history object: %s " % (new_history))
        return new_history

    def get_active_time(self, earliest_time=None, latest_time=None):
        """
        A set of filters used to determine the amount of 'active time'
        earliest_time and latest_time are taken into account, if provided.
        """

        # When to start counting
        if earliest_time and self.start_date <= earliest_time:
            start_time = earliest_time
        else:
            start_time = self.start_date

        # When to stop counting.. Some history may have no end date!
        if latest_time:
            if not self.end_date or self.end_date >= latest_time:
                final_time = latest_time
                # TODO: Possibly check latest_time < timezone.now() to prevent
                #      bad input?
            else:
                final_time = self.end_date
        elif self.end_date:
            # Final time is end date, because NOW is being used
            # as the 'counter'
            final_time = self.end_date
        else:
            # This is the current status, so stop counting now..
            final_time = timezone.now()

        # Sanity checks are important.
        # Inactive states are not counted against you.
        if not self.is_active():
            return (timedelta(), start_time, final_time)
        if self.start_date > final_time:
            return (timedelta(), start_time, final_time)
        # Active time is easy now!
        active_time = final_time - start_time
        return (active_time, start_time, final_time)

    def __unicode__(self):
        return "%s (FROM:%s TO:%s)" % (
            self.status, self.start_date, self.end_date if self.end_date else ''
        )

    def is_active(self):
        """
        Use this function to determine whether or not a specific instance
        status history should be considered 'active'
        """
        # Running is legacy
        if self.status.name == 'active' or self.status.name == 'running':
            return True
        else:
            return False

    class Meta:
        db_table = "instance_status_history"
        app_label = "core"
