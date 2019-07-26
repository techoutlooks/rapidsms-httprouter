import json

from django import forms
from django.http import HttpResponse
from django.template import RequestContext
from django.shortcuts import render
from django.conf import settings
from django.db.models import Q
from django.core.paginator import *
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from rapidsms.messages.outgoing import OutgoingMessage
from rapidsms.models import Connection
from djtables import Table, Column
from djtables.column import DateColumn

from django.core.mail import send_mail
import datetime

from .models import Message
from .router import get_router

class SecureForm(forms.Form):
    """
    Abstracts out requirement of a password.  If you have a password set
    in settings.py, then this will make sure it is included in all outbox,
    receive and delivered calls.
    """
    password = forms.CharField(required=False)

    def clean(self):
        # if a password required in our settings
        password = getattr(settings, "ROUTER_PASSWORD", None)
        if password:
            if not 'password' in self.cleaned_data or self.cleaned_data['password'] != password:
                raise forms.ValidationError("You must specify a valid password.")

        return self.cleaned_data

class MessageForm(SecureForm):
    backend = forms.CharField(max_length=32)
    sender = forms.CharField(max_length=20)
    message = forms.CharField(max_length=160, required=False)
    echo = forms.BooleanField(required=False)

class OutboxForm(SecureForm):
    backend = forms.CharField(max_length=32, required=False)

def receive(request):
    """
    Takes the passed in message.  Creates a record for it, and passes it through
    all the rapidsms applications for processing.
    """
    form = MessageForm(request.GET)

    # missing fields, fail
    if not form.is_valid():
        return HttpResponse(str(form.errors), status=400)

    # otherwise, create the message
    data = form.cleaned_data
    router = get_router()
    message = router.handle_incoming(data['backend'], data['sender'], data['message'])

    response = {}
    response['message'] = message.as_json()
    response['responses'] = [m.as_json() for m in message.responses.all()]
    response['status'] = "Message handled."

    # do we default to having silent responses?  200 means success in this case
    if getattr(settings, "ROUTER_SILENT", False) and (not 'echo' in data or not data['echo']):
        return HttpResponse()
    else:
        return HttpResponse(json.dumps(response))


@csrf_exempt
def relaylog(request):
    """
    Used by relay apps to send a log of their status.  The send in log is forwarded by email to the
    system administrators.

    DEPRECATED: should be replaced with alert calls below
    """
    password = getattr(settings, "ROUTER_PASSWORD", None)

    if request.method == 'POST' and 'log' in request.POST and 'password' in request.POST and request.POST['password'] == password:
        send_mail('Relay Log',
                  request.POST['log'], settings.DEFAULT_FROM_EMAIL,
                  [admin[1] for admin in settings.ADMINS], fail_silently=False)

        return HttpResponse("Log Sent")
    else:
        return HttpResponse("Must be POST of [log, password]", status=400)


@csrf_exempt
def alert(request):
    """
    Used by relay apps to send email alerts or messages to administrators.
    """
    password = getattr(settings, "ROUTER_PASSWORD", None)

    if request.method == 'POST' and 'body' in request.POST and 'subject' in request.POST:
        if not password or request.POST.get('password', None) == password:
            send_mail(request.POST['subject'],
                      request.POST['body'], settings.DEFAULT_FROM_EMAIL,
                      [admin[1] for admin in settings.ADMINS], fail_silently=False)

            return HttpResponse("Log Sent")
        else:
            return HttpResponse("Incorrect password.")
    else:
        return HttpResponse("Must be POST containing subject, body and password params", status=400)


def outbox(request):
    """
    Returns any messages which have been queued to be sent but have no yet been marked
    as being delivered.
    """
    form = OutboxForm(request.GET)
    if not form.is_valid():
        return HttpResponse(str(form.errors), status=400)

    data = form.cleaned_data
    pending_messages = Message.objects.filter(status='Q')
    if 'backend' in data and data['backend']:
        pending_messages = pending_messages.filter(connection__backend__name__iexact=data['backend'])

    response = {}
    messages = []
    for message in pending_messages:
        messages.append(message.as_json())

    response['outbox'] = messages
    response['status'] = "Outbox follows."

    return HttpResponse(json.dumps(response))


class DeliveredForm(SecureForm):
    message_id = forms.IntegerField()


def delivered(request):
    """
    Called when a message is delivered by our backend.
    """
    form = DeliveredForm(request.GET)

    if not form.is_valid():
        return HttpResponse(str(form.errors), status=400)

    get_router().mark_delivered(form.cleaned_data['message_id'])

    return HttpResponse(json.dumps(dict(status="Message marked as sent.")))


class MessageTable(Table):
    # this is temporary, until i fix ModelTable!
    text = Column()
    direction = Column()
    connection = Column(link=lambda cell: "javascript:reply('%s')" % cell.row.connection.identity)
    status = Column()
    date = DateColumn(format="m/d/Y H:i:s")

    class Meta:
        order_by = '-date'


class SendForm(forms.Form):
    sender = forms.CharField(max_length=20, initial="12065551212")
    text = forms.CharField(max_length=160, label="Message", widget=forms.TextInput(attrs={'size': '60'}))


class ReplyForm(forms.Form):
    recipient = forms.CharField(max_length=20)
    message = forms.CharField(max_length=160, widget=forms.TextInput(attrs={'size': '60'}))


class SearchForm(forms.Form):
    search = forms.CharField(label="Keywords", max_length=100, widget=forms.TextInput(attrs={'size': '60'}), required=False)

def status(request):
    """
    Simple view suitable for automated monitoring, will output how many messages have been pending to send for a
    while.
    """
    fifteen_minutes_ago = timezone.now() - datetime.timedelta(minutes=15)
    pending_count = Message.objects.filter(status='Q', date__lte=fifteen_minutes_ago).count()

    return render(request, "router/status.html", dict(pending_count=pending_count))

def console(request):
    """
    Our web console, lets you see recent messages as well as send out new ones for
    processing.
    """
    form = SendForm()
    reply_form = ReplyForm()
    search_form = SearchForm()

    queryset = Message.objects.all()

    if request.method == 'POST':
        if request.POST['action'] == 'test':
            form = SendForm(request.POST)
            if form.is_valid():
                backend = "console"
                message = get_router().handle_incoming(backend,
                                                       form.cleaned_data['sender'],
                                                       form.cleaned_data['text'])
            reply_form = ReplyForm()

        elif request.POST['action'] == 'reply':
            reply_form = ReplyForm(request.POST)
            if reply_form.is_valid():
                if Connection.objects.filter(identity=reply_form.cleaned_data['recipient']).count():
                    text = reply_form.cleaned_data['message']
                    conn = Connection.objects.filter(identity=reply_form.cleaned_data['recipient'])[0]
                    outgoing = OutgoingMessage(conn, text)
                    get_router().handle_outgoing(outgoing)
                else:
                    reply_form.errors.setdefault('short_description', ErrorList())
                    reply_form.errors['recipient'].append("This number isn't in the system")

    if request.GET.get('action', None) == 'search':
        # split on spaces
        search_form = SearchForm(request.GET)
        if search_form.is_valid():
            terms = search_form.cleaned_data['search'].split()

            if terms:
                term = terms[0]
                query = (Q(text__icontains=term) | Q(in_response_to__text__icontains=term) | Q(connection__identity__icontains=term))
                for term in terms[1:]:
                    query &= (Q(text__icontains=term) | Q(in_response_to__text__icontains=term) | Q(connection__identity__icontains=term))

                queryset = queryset.filter(query)

    paginator = Paginator(queryset.order_by('-id'), 20)
    page = request.GET.get('page')
    try:
        messages = paginator.page(page)
    except EmptyPage:
        # If page is out of range (e.g. 9999), deliver last page of results.
        messages = paginator.page(paginator.num_pages)
    except:
        # None or not an integer, default to first page
        messages = paginator.page(1)

    return render(
        request,
        "router/index.html", {
            "messages_table": MessageTable(queryset, request=request),
            "form": form,
            "reply_form": reply_form,
            "search_form": search_form,
            "sms_messages": messages
        }
    )
