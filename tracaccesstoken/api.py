# -*- coding: utf-8 -*-

import hashlib
import json

from trac.core import *
from trac.core import ExtensionPoint
from trac.ticket.model import Milestone, Ticket
from trac.ticket.notification import TicketNotifyEmail
from trac.perm import PermissionSystem
from trac.perm import IPermissionGroupProvider
from trac.util.datefmt import (
    get_date_format_hint, get_datetime_format_hint, to_utimestamp,
    user_time, utc, to_datetime
)
from trac.web.main import IRequestHandler
from tracaccesstoken.constants import NAME_RPC_TIMESTAMP

__all__ = ['TicketAPI']


class TicketAPI(Component):
    """ An interface to Trac's ticketing system. """
    implements(
        IRequestHandler,
        # ITemplateProvider,
        # INavigationContributor
    )
    group_providers = ExtensionPoint(IPermissionGroupProvider)

    # IRequestHandler methods
    def match_request(self, req):
        return '/api/tickets' in req.path_info

    def process_request(self, req):
        self._process_new_ticket_request(req)

    # Internal methods

    @staticmethod
    def _authorization(req):
        return req.args.get('Authorization') or req.get_header('Authorization')

    def _process_new_ticket_request(self, req):
        if req.method == 'POST':
            content_type = req.get_header('Content-Type') or 'application/json'

            authorization = self._authorization(req)
            if not authorization:
                content = {
                    'message': 'Empty credentials',
                    'description': "The access token is incorrect."
                }
                req.send_response(401)
                req.send_header('Content-Type', content_type)
                req.send_header('Content-Length', len(json.dumps(content)))
                req.end_headers()
                req.write(json.dumps(content))
                return None
            else:
                access_token = str(authorization).replace('token ', '').strip()
                authname = ''
                for username in self.env.db_query("""
                    SELECT username
                    FROM kkbox_trac_access_token
                    WHERE access_token=%s
                    ORDER BY create_time DESC
                    """, (hashlib.sha224(access_token).hexdigest(),)):
                    authname = username
                if not authname:
                    content = {
                        'message': 'Bad credentials',
                        'description': "The access token is incorrect."
                    }
                    req.send_response(401)
                    req.send_header('Content-Type', content_type)
                    req.send_header('Content-Length', len(json.dumps(content)))
                    req.end_headers()
                    req.write(json.dumps(content))
                    return None

            allow_create_ticket = 'TICKET_CREATE' in self._get_groups(authname)
            if allow_create_ticket:
                content = {
                    'message': 'forbidden',
                    'description': "%s privileges are required to perform this operation. "
                                   "You don't have the required permissions." % 'TICKET_CREATE'
                }
                req.send_response(403)
                req.send_header('Content-Type', content_type)
                req.send_header('Content-Length', len(json.dumps(content)))
                req.end_headers()
                req.write(json.dumps(content))
                return None

            try:
                ticket_id = self._create(req, authname)
                content = {
                    'ticket_id': ticket_id
                }
                req.send_response(201)
            except ValueError as ex:
                self.log.error('_create() failed. %s', ex)
                content = {
                    'message': 'invalid_json_value',
                    'description': 'Invalid request body'
                }
                req.send_response(400)
            except Exception as ex:
                self.log.error('_create() failed. %s', ex)
                content = {
                    'message': 'invalid_json_value',
                    'description': 'Invalid request body'
                }
                req.send_response(400)

            # Build response
            req.send_header('Content-Type', content_type)
            req.send_header('Content-Length', len(json.dumps(content)))
            req.end_headers()
            req.write(json.dumps(content))
        else:
            pass

    def _create(self, req, authname_):
        """ Create a new ticket, returning the ticket ID.
        Overriding 'when' requires admin permission. """

        content_len = int(req.get_header('content-length') or 0)

        # Read request body
        post_body = json.loads(req.read(content_len))
        self.log.debug('BODY=%s' % post_body)

        # Prepare props
        summary = post_body['summary'] or ''
        description = ''
        author = post_body['author'] or authname_
        reporter = post_body['reporter'] or authname_
        attributes = {}
        for key, value in post_body.iteritems():
            attributes[key] = value
        notify = False
        when = None

        # Validate inputs
        if not summary:
            raise ValueError('Empty field. Field name = summary')
        if not author:
            raise ValueError('Empty field. Field name = author')

        # Prepare ticket
        t = Ticket(self.env)
        t['summary'] = summary
        t['description'] = description
        t['reporter'] = reporter
        for k, v in attributes.iteritems():
            t[k] = v
        t['status'] = 'new'
        t['resolution'] = ''
        # custom author?
        if author and not (req.authname == 'anonymous' or 'TICKET_ADMIN' in req.perm(t.resource)):
            # only allow custom author if anonymous is permitted or user is admin
            self.log.warn("RPC ticket.create: %r not allowed to change author "
                          "to %r for comment on #%d", req.authname, author, id)
            author = ''
        t['author'] = author or req.authname

        # custom create timestamp?
        when = when or getattr(req, NAME_RPC_TIMESTAMP, None)
        if when and 'TICKET_ADMIN' not in req.perm:
            self.log.warn("RPC ticket.create: %r not allowed to create with "
                          "non-current timestamp (%r)", req.authname, when)
            when = None
        when = when or to_datetime(None, utc)
        t.insert(when=when)
        if notify:
            try:
                tn = TicketNotifyEmail(self.env)
                tn.notify(t, newticket=True)
            except Exception, e:
                self.log.exception("Failure sending notification on creation "
                                   "of ticket #%s: %s" % (t.id, e))
        return t.id

    def _get_groups(self, user):
        groups = set([user])
        for provider in self.group_providers:
            for group in provider.get_permission_groups(user):
                groups.add(group)

        perms = PermissionSystem(self.env).get_user_permissions(user)
        repeat = True
        while repeat:
            repeat = False
            for action in perms:
                if action in groups and not action.isupper() and action not in groups:
                    groups.add(action)
                    repeat = True

        self.log.debug(groups)
        return groups
