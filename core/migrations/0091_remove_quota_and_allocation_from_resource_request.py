# -*- coding: utf-8 -*-
# Generated by Django 1.10.6 on 2017-07-18 23:08
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0090_project_resources_renamed'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='resourcerequest',
            name='allocation',
        ),
        migrations.RemoveField(
            model_name='resourcerequest',
            name='quota',
        ),
    ]
