from django.conf import settings
from django.core.mail import send_mail
from django.core.urlresolvers import reverse
from django.template import Context
from django.template.loader import get_template

from twilio.rest import TwilioRestClient
from twilio import twiml
import requests
import logging

logger = logging.getLogger(__name__)


def send_alert(service, duty_officers=None):
    users = service.users_to_notify.all()
    if service.email_alert:
        send_email_alert(service, users, duty_officers)
    if service.hipchat_alert:
        send_hipchat_alert(service, users, duty_officers)
    if service.sms_alert:
        send_sms_alert(service, users, duty_officers)
    if service.telephone_alert:
        send_telephone_alert(service, users, duty_officers)


def get_first_fail(service):
    still_failing = service.overall_status != service.PASSING_STATUS
    try:
        last_passing = service.snapshots.filter(
            overall_status=service.PASSING_STATUS
        ).order_by('-id')[0 if still_failing else 1]
    except IndexError:
        failing_since = service.snapshots.order_by('id')[0]
    else:
        failing_since = service.snapshots.filter(id__gt=last_passing.id).order_by('id')[0]

    return failing_since


def send_email_alert(service, users, duty_officers):
    emails = [u.email for u in users if u.email]
    if not emails:
        return
    c = Context({
        'service': service,
        'host': settings.WWW_HTTP_HOST,
        'scheme': settings.WWW_SCHEME,
        'first_fail': get_first_fail(service),
    })
    if service.overall_status != service.PASSING_STATUS:
        if service.overall_status == service.CRITICAL_STATUS:
            emails += [u.email for u in duty_officers]
        subject = '%s status for service: %s' % (
            service.overall_status, service.name)
    else:
        subject = 'Service back to normal: %s' % (service.name,)
    send_mail(
        subject=subject,
        message=get_template('cabotapp/alert_email.html').render(c),
        from_email='Cabot <%s>' % settings.CABOT_FROM_EMAIL,
        recipient_list=emails,
    )


def send_hipchat_alert(service, users, duty_officers):
    alert = True
    hipchat_aliases = [u.profile.hipchat_alias for u in users if hasattr(
        u, 'profile') and u.profile.hipchat_alias]
    if service.overall_status == service.WARNING_STATUS:
        alert = False  # Don't alert at all for WARNING
    if service.overall_status == service.ERROR_STATUS:
        if service.old_overall_status in (service.ERROR_STATUS, service.ERROR_STATUS):
            alert = False  # Don't alert repeatedly for ERROR
    if service.overall_status == service.PASSING_STATUS:
        color = 'green'
        if service.old_overall_status == service.WARNING_STATUS:
            alert = False  # Don't alert for recovery from WARNING status
    else:
        color = 'red'
        if service.overall_status == service.CRITICAL_STATUS:
            hipchat_aliases += [u.profile.hipchat_alias for u in duty_officers if hasattr(
                u, 'profile') and u.profile.hipchat_alias]
    c = Context({
        'service': service,
        'users': hipchat_aliases,
        'host': settings.WWW_HTTP_HOST,
        'scheme': settings.WWW_SCHEME,
        'alert': alert,
        'first_fail': get_first_fail(service),
    })
    message = get_template('cabotapp/alert_hipchat.txt').render(c)
    _send_hipchat_alert(message, color=color, sender='Cabot/%s' % service.name)


def _send_hipchat_alert(message, color='green', sender='Cabot'):
    room = settings.HIPCHAT_ALERT_ROOM
    api_key = settings.HIPCHAT_API_KEY
    url = settings.HIPCHAT_URL
    resp = requests.post(url + '?auth_token=' + api_key, data={
        'room_id': room,
        'from': sender[:15],
        'message': message,
        'notify': 1,
        'color': color,
        'message_format': 'text',
    })


def send_sms_alert(service, users, duty_officers):
    client = TwilioRestClient(
        settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    mobiles = [u.profile.prefixed_mobile_number for u in users if hasattr(
        u, 'profile') and u.profile.mobile_number]
    if service.is_critical:
        mobiles += [u.profile.prefixed_mobile_number for u in duty_officers if hasattr(
            u, 'profile') and u.profile.mobile_number]
    c = Context({
        'service': service,
        'host': settings.WWW_HTTP_HOST,
        'scheme': settings.WWW_SCHEME,
        'first_fail': get_first_fail(service),
    })
    message = get_template('cabotapp/alert_sms.txt').render(c)
    mobiles = list(set(mobiles))
    for mobile in mobiles:
        try:
            client.sms.messages.create(
                to=mobile,
                from_=settings.TWILIO_OUTGOING_NUMBER,
                body=message,
            )
        except Exception, e:
            logger.exception('Error sending twilio sms: %s' % e)


def send_telephone_alert(service, users, duty_officers):
    # No need to call to say things are resolved
    if service.overall_status != service.CRITICAL_STATUS:
        return
    client = TwilioRestClient(
        settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    mobiles = [u.profile.prefixed_mobile_number for u in duty_officers if hasattr(
        u, 'profile') and u.profile.mobile_number]
    url = 'http://%s%s' % (settings.WWW_HTTP_HOST,
                           reverse('twiml-callback', kwargs={'service_id': service.id}))
    for mobile in mobiles:
        try:
            client.calls.create(
                to=mobile,
                from_=settings.TWILIO_OUTGOING_NUMBER,
                url=url,
                method='GET',
            )
        except Exception, e:
            logger.exception('Error making twilio phone call: %s' % e)


def telephone_alert_twiml_callback(service):
    c = Context({'service': service})
    t = get_template('cabotapp/alert_telephone.txt').render(c)
    r = twiml.Response()
    r.say(t, voice='woman')
    r.hangup()
    return r
