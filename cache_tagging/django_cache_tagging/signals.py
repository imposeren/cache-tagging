# -*- coding: utf-8 -*-
import django.dispatch

tag_invalidated = django.dispatch.Signal(providing_args=['tag', 'version'])
