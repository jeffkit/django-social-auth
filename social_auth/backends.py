"""
Authentication backeds for django.contrib.auth AUTHENTICATION_BACKENDS setting
"""
from os import urandom

from openid.extensions import ax, sreg

from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.utils.hashcompat import md5_constructor

from .models import UserSocialAuth
from .conf import OLD_AX_ATTRS, AX_SCHEMA_ATTRS
from .signals import pre_update

USERNAME = 'username'

# get User class, could not be auth.User
User = UserSocialAuth._meta.get_field('user').rel.to


class SocialAuthBackend(ModelBackend):
    """A django.contrib.auth backend that authenticates the user based on
    a authentication provider response"""
    name = ''  # provider name, it's stored in database

    def authenticate(self, *args, **kwargs):
        """Authenticate user using social credentials

        Authentication is made if this is the correct backend, backend
        verification is made by kwargs inspection for current backend
        name presence.
        """
        # Validate backend and arguments. Require that the OAuth response
        # be passed in as a keyword argument, to make sure we don't match
        # the username/password calling conventions of authenticate.
        if not (self.name and kwargs.get(self.name) and 'response' in kwargs):
            return None

        response = kwargs.get('response')
        details = self.get_user_details(response)
        uid = self.get_user_id(details, response)
        new_user = False
        try:
            social_user = UserSocialAuth.objects.select_related('user')\
                                                .get(provider=self.name,
                                                     uid=uid)
        except UserSocialAuth.DoesNotExist:
            user = kwargs.get('user')
            if user is None:  # new user
                if not getattr(settings, 'SOCIAL_AUTH_CREATE_USERS', True):
                    return None
                username = self.username(details)
                email = details.get('email')
                user = User.objects.create_user(username=username, email=email)
                new_user = True
            social_user = self.associate_auth(user, uid, response, details)
        else:
            user = social_user.user

        self.update_user_details(user, response, details, new_user=new_user)
        return user

    def username(self, details):
        """Return an unique username, if SOCIAL_AUTH_FORCE_RANDOM_USERNAME
        setting is True, then username will be a random 30 chars md5 hash
        """
        def get_random_username():
            """Return hash from random string cut at 30 chars"""
            return md5_constructor(urandom(10)).hexdigest()[:30]

        if getattr(settings, 'SOCIAL_AUTH_FORCE_RANDOM_USERNAME', False):
            username = get_random_username()
        elif USERNAME in details:
            username = details[USERNAME]
        elif hasattr(settings, 'SOCIAL_AUTH_DEFAULT_USERNAME'):
            username = settings.SOCIAL_AUTH_DEFAULT_USERNAME
            if callable(username):
                username = username()
        else:
            username = get_random_username()

        name, idx = username, 2
        while True:
            try:
                User.objects.get(username=name)
                name = username + str(idx)
                idx += 1
            except User.DoesNotExist:
                username = name
                break
        return username

    def associate_auth(self, user, uid, response, details):
        """Associate a Social Auth with an user account."""
        extra_data = '' if not getattr(settings, 'SOCIAL_AUTH_EXTRA_DATA',
                                       False) \
                        else self.extra_data(user, uid, response, details)
        return UserSocialAuth.objects.create(user=user, uid=uid,
                                             provider=self.name,
                                             extra_data=extra_data)

    def extra_data(self, user, uid, response, details):
        """Return default blank user extra data"""
        return ''

    def update_user_details(self, user, response, details, new_user=False):
        """Update user details with (maybe) new data. Username is not
        changed if associating a new credential."""
        changed = False
        for name, value in details.iteritems():
            # not update username if user already exists
            if not new_user and name == USERNAME:
                continue
            if value and value != getattr(user, name, value):
                setattr(user, name, value)
                changed = True

        # Fire a pre-update signal sending current backend instance,
        # user instance (created or retrieved from database), service
        # response and processed details, signal handlers must return
        # True or False to signal that something has changed
        updated = filter(None, pre_update.send(sender=self, user=user,
                                               response=response,
                                               details=details))
        if changed or len(updated) > 0:
            user.save()

    def get_user_id(self, details, response):
        """Must return a unique ID from values returned on details"""
        raise NotImplementedError('Implement in subclass')

    def get_user_details(self, response):
        """Must return user details in a know internal struct:
            {USERNAME: <username if any>,
             'email': <user email if any>,
             'fullname': <user full name if any>,
             'first_name': <user first name if any>,
             'last_name': <user last name if any>}
        """
        raise NotImplementedError('Implement in subclass')

    def get_user(self, user_id):
        """Return user instance for @user_id"""
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None


class OAuthBackend(SocialAuthBackend):
    """OAuth authentication backend base class"""
    def get_user_id(self, details, response):
        "OAuth providers return an unique user id in response"""
        return response['id']

    def extra_data(self, user, uid, response, details):
        """Return access_token to store in extra_data field"""
        return response.get('access_token', '')


class TwitterBackend(OAuthBackend):
    """Twitter OAuth authentication backend"""
    name = 'twitter'

    def get_user_details(self, response):
        """Return user details from Twitter account"""
        return {USERNAME: response['screen_name'],
                'email': '',  # not supplied
                'fullname': response['name'],
                'first_name': response['name'],
                'last_name': ''}


class OrkutBackend(OAuthBackend):
    """Orkut OAuth authentication backend"""
    name = 'orkut'

    def get_user_details(self, response):
        """Return user details from Orkut account"""
        return {USERNAME: response['displayName'],
                'email': response['emails'][0]['value'],
                'fullname': response['displayName'],
                'firstname': response['name']['givenName'],
                'lastname': response['name']['familyName']}


class FacebookBackend(OAuthBackend):
    """Facebook OAuth authentication backend"""
    name = 'facebook'

    def get_user_details(self, response):
        """Return user details from Facebook account"""
        return {USERNAME: response['name'],
                'email': response.get('email', ''),
                'fullname': response['name'],
                'first_name': response.get('first_name', ''),
                'last_name': response.get('last_name', '')}


class OpenIDBackend(SocialAuthBackend):
    """Generic OpenID authentication backend"""
    name = 'openid'

    def get_user_id(self, details, response):
        """Return user unique id provided by service"""
        return response.identity_url

    def get_user_details(self, response):
        """Return user details from an OpenID request"""
        values = {USERNAME: '', 'email': '', 'fullname': '',
                  'first_name': '', 'last_name': ''}

        resp = sreg.SRegResponse.fromSuccessResponse(response)
        if resp:
            values.update((name, resp.get(name) or values.get(name) or '')
                                for name in ('email', 'fullname', 'nickname'))

        # Use Attribute Exchange attributes if provided
        resp = ax.FetchResponse.fromSuccessResponse(response)
        if resp:
            values.update((alias.replace('old_', ''), resp.getSingle(src))
                            for src, alias in OLD_AX_ATTRS + AX_SCHEMA_ATTRS)

        fullname = values.get('fullname') or ''
        first_name = values.get('first_name') or ''
        last_name = values.get('last_name') or ''

        if not fullname and first_name and last_name:
            fullname = first_name + ' ' + last_name
        elif fullname:
            try:  # Try to split name for django user storage
                first_name, last_name = fullname.rsplit(' ', 1)
            except ValueError:
                last_name = fullname

        values.update({'fullname': fullname, 'first_name': first_name,
                       'last_name': last_name,
                       USERNAME: values.get(USERNAME) or \
                                   (first_name.title() + last_name.title())})
        return values


class GoogleBackend(OpenIDBackend):
    """Google OpenID authentication backend"""
    name = 'google'


class YahooBackend(OpenIDBackend):
    """Yahoo OpenID authentication backend"""
    name = 'yahoo'
