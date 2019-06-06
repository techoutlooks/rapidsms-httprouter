#!/usr/bin/env python
# vim: ai ts=4 sts=4 et sw=4

from django.conf.urls import *
from .views import receive, outbox, delivered, console, relaylog, alert, status
from .textit import textit_webhook
from django.contrib.admin.views.decorators import staff_member_required

urlpatterns = [
   url("^router/status", status),
   url("^router/receive", receive),
   url("^router/outbox", outbox),
   url("^router/relaylog", relaylog),
   url("^router/alert", alert),
   url("^router/delivered", delivered),
   url("^router/console", staff_member_required(console), {}, 'httprouter-console'),
   url("^router/textit", textit_webhook),
]
