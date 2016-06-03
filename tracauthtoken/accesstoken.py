"""
accesstoken.py - Advanced Access Token Generator Plugin for Trac

This module defines a Trac extension point for generating access tokens backend.

See TracTracAccessTokenBackend for more details.
"""

import itertools
from operator import itemgetter
import pkg_resources
import re

try:
    import simplejson as json
except ImportError:
    import json

from trac.perm import IPermissionRequestor
from trac.ticket.api import ITicketChangeListener
from trac.web.chrome import INavigationContributor
from trac.web.chrome import ITemplateProvider
from trac.web.main import IRequestHandler
from trac.prefs import IPreferencePanelProvider

from genshi.builder import tag, Element
from interface import IAdvSearchBackend
from trac.core import Component
from trac.core import ExtensionPoint
from trac.core import implements
from trac.mimeview import Context
from trac.perm import PermissionSystem
from trac.perm import IPermissionGroupProvider
from trac.util.html import html
from trac.util.presentation import Paginator
from trac.util.translation import _
from trac.web.chrome import add_stylesheet, add_warning, add_script, add_notice
from trac.wiki.formatter import extract_link

import operator

PACKAGE = 'tracauthtoken'
CONFIG_SECTION_NAME = 'auth_token_plugin'
CONFIG_FIELD = {
    'menu_label': (
        CONFIG_SECTION_NAME,
        'menu_label',
        'Access Tokens',
    ),
    'ticket_status': (
        CONFIG_SECTION_NAME,
        'ticket_status',
        'new, accepted, assigned, reopened, closed',
    ),
    'ticket_status_enable': (
        CONFIG_SECTION_NAME,
        'ticket_status_enable',
        'new, accepted, assigned, reopened, closed',
    ),
    'insensitive_group': (
        CONFIG_SECTION_NAME,
        'insensitive_group',
        'intern,outsourcing',
    ),
    'sensitive_keyword': (
        CONFIG_SECTION_NAME,
        'isensitive_keyword',
        'secret',
    ),
}
# --- any() from Python 2.5 ---
try:
    from __builtin__ import any
except ImportError:
    def any(items):
        for item in items:
            if item:
                return True
        return False

# ---all() from Python 2.5 ---
try:
    from __builtin__ import all
except ImportError:
    def all(items):
        return reduce(operator.__and__, items)

__all__ = ("any", "all")


def _get_config_values(config, option_name):
    values = config.get(*CONFIG_FIELD[option_name])
    return [value.strip() for value in values.split(',')]


class AccessTokenBackendException(Exception):
    """
    Raised by Access Token Backends when there is a problem generating the access token.
    """


class AccessTokenBackendPlugin(Component):
    implements(
        # INavigationContributor,
        # IPermissionRequestor,
        IRequestHandler,
        ITemplateProvider,
        IPreferencePanelProvider
    )
    
    providers = ExtensionPoint(IAdvSearchBackend)
    group_providers = ExtensionPoint(IPermissionGroupProvider)

    DEFAULT_PER_PAGE = 15

    def __init__(self, arg=None):
        self.new_token = None

    def _get_source_filters(self):
        return set(itertools.chain(*(p.get_sources() for p in self.providers)))

    # INavigationContributor methods
    def get_active_navigation_item(self, req):
        return 'accesstoken'

    def get_navigation_items(self, req):
        if 'SEARCH_VIEW' in req.perm:
            label = self.config.get(*CONFIG_FIELD['menu_label'])
            yield ('mainnav',
                   'accesstoken',
                   html.A(_(label), href=self.env.href.advsearch())
            )

    # IPermissionRequestor methods
    def get_permission_actions(self):
        return ['SEARCH_VIEW']

    # IRequestHandler methods
    def match_request(self, req):
        # TODO: add /search if search module is disabled
        return re.match(r'/accesstoken?', req.path_info) is not None 

    def process_request(self, req):
        """
        Implements IRequestHandler.process_request

        Build a dict of search criteria from the user and request results from
        the active AdvancedSearchBackend.
        """
        req.perm.assert_permission('SEARCH_VIEW')
        self.log.debug(req.perm.assert_permission('SEARCH_VIEW'))

        try:
            per_page = int(req.args.getfirst('per_page',
                self.DEFAULT_PER_PAGE))
        except ValueError:
            self.log.warn('Could not set per_page to %s' %
                    req.args.getfirst('per_page'))
            per_page = self.DEFAULT_PER_PAGE

        try:
            page = int(req.args.getfirst('page', 1))
        except ValueError:
            page = 1

        sort_order = req.args.getfirst('sort_order', 'relevance')

        perms = PermissionSystem(self.env).get_user_permissions(req.perm.username)
        data = {
            'source': self._get_filter_dicts(req.args),
            'author': [auth for auth in req.args.getlist('author') if auth],
            'date_start': req.args.getfirst('date_start'),
            'date_end': req.args.getfirst('date_end'),
            'q': req.args.get('q'),
            'start_points': StartPoints.parse_args(req.args, self.providers),
            'per_page': per_page,
            'sort_order': sort_order,
            'ticket_statuses': self._get_ticket_statuses(req.args),
            'from': req.args.getfirst('from'),
            'username': req.perm.username,
            'perms': perms
        }

        # Initial page request
        if not any((data['q'], data['author'], data['date_start'], data['date_end'])):
            return self._send_response(req, data)

        # Look for quickjump
        quickjump = self._get_quickjump(req, data['q'])
        if quickjump:
            req.redirect(quickjump)

        self.log.debug(data)
        # perform query using backend if q is set
        result_map = {}
        total_count = 0
        for provider in self.providers:
            result_count, result_list = 0, []
            try:
                result_count, result_list = provider.query_backend(data)
            except AccessTokenBackendException, e:
                add_warning(req, _('AccessTokenBackendException: %s' % e))
            total_count += result_count
            result_map[provider.get_name()] = result_list

        if not total_count:
            return self._send_response(req, data)

        data['page'] = page
        results = self._merge_results(result_map, per_page)
        self._add_href_to_results(results)
        data['results'] = Paginator(
            results,
            page=page-1,
            max_per_page=per_page,
            num_items=total_count
        )

        # pagination next/prev links
        #if data['results'].has_next_page:
        self.log.debug('%s %s', len(results), data['start_points'])
        data['start_points'] = StartPoints.format(results, data['start_points'])

        return self._send_response(req, data)

    def _send_response(self, req, data):
        """Send the response."""

        # look for warnings
        if not len(self.providers):
            add_warning(req, _('No advanced search providers found. ' +
                        'You must register a search backend.'))

        if data.get('results') and not len(data['results']):
            add_warning(req, _('No results.'))

        add_stylesheet(req, 'common/css/search.css')
        add_stylesheet(req, PACKAGE + '/css/advsearch.css')
        add_stylesheet(req, PACKAGE + '/css/pikaday.css')
        add_script(req, PACKAGE + '/js/advsearch.js')
        add_script(req, PACKAGE + '/js/pikaday.js')
        return 'prefs_tokens.html', data, None

    def _merge_results(self, result_map, per_page):
        """
        Merge results from multiple sources by score in each result. Return
        the search results to display to the user

        Example:
        [
            {
                'title': 'Trac Help',
                'href': 'http://...',
                'date': '2011-04-20 12:34:00',
                'author': 'admin',
                'summary': '...'
            },
            ...
        ]
        """
        # add backend_name as a key to each result and merge lists
        all_results = []
        for backend_name, results in result_map.iteritems():
            for result_dict in results:
                result_dict['backend_name'] = backend_name
            all_results.extend(results)

        # sort and return results for the page
        all_results.sort(key=itemgetter('score'), reverse=True)
        return all_results[:per_page]

    def _add_href_to_results(self, results):
        """Add an href key/value to each result dict based on source."""
        for result in results:
            if result['source'] == 'wiki':
                result['href'] = self.env.href.wiki(result['title'])
            if result['source'] == 'ticket':
                result['href'] = self.env.href.ticket(result['ticket_id'])

    def _get_filter_dicts(self, req_args):
        """Map filters to filter dicts for the frontend."""
        return [
            {'name': filter, 'active': req_args.get(filter)}
            for filter in self._get_source_filters()
        ]

    def _get_ticket_statuses(self, req_args):
        """Create map of ticket statuses."""
        status_values = _get_config_values(self.config, 'ticket_status')
        statuses = []

        # Default to new/assigned/reopened
        defaults = set(_get_config_values(self.config, 'ticket_status_enable'))
        if any((req_args.get('status_%s' % s) for s in status_values)):
            defaults = set()

        for status in status_values:
            field_name = 'status_%s' % status
            statuses.append({
                'name': status,
                'active': req_args.get(field_name) or (status in defaults),
                'field_name': field_name,
            })
        return statuses

    def _get_quickjump(self, req, query):
        """Find quickjump requests if the search comes from the searchbox
        in the header.  The search is assumed to be from the header searchbox
        if no page or per_page arguments are found.
        """
        if req.args.get('page') or req.args.get('per_page'):
            return None

        link = extract_link(self.env,
                Context.from_request(req, PACKAGE), query)
        if isinstance(link, Element):
            return link.attrib.get('href')

    # ITemplateProvider methods
    def get_htdocs_dirs(self):
        return [('tracauthtoken', pkg_resources.resource_filename(__name__, 'htdocs'))]

    def get_templates_dirs(self):
        return [pkg_resources.resource_filename(__name__, 'templates')]

    # IPreferencePanelProvider methods
    def get_preference_panels(self, req):
        """
        Implement IPreferencePanelProvider.get_preference_panels method

        Add access token entry in Preferences
        """
        label = self.config.get(*CONFIG_FIELD['menu_label'])
        yield ('accesstoken', label)

    def render_preference_panel(self, req, panel):
        """
        Implement IPreferencePanelProvider.render_preference_panel method

        Add request handler for accesstoken POST request
        """

        add_stylesheet(req, PACKAGE + '/css/advsearch.css')
        add_script(req, PACKAGE + '/js/advsearch.js')

        new_token = []
        if req.method == 'POST':
            new_token = req.args.get('tokens')
            if new_token:
                add_notice(req, _('Your access tokens have been saved.'))
        self.log.debug("*" * 30  + json.dumps(new_token))
        return 'prefs_tokens.html', {
            'tokens': new_token
        }

    def _get_groups(self, user):
        groups = set([user])
        for provider in self.group_providers:
            for group in provider.get_permission_groups(user):
                groups.add(group)

            #groups += PermissionSystem(self.env).get_user_permissions(user)

        perms = PermissionSystem(self.env).get_user_permission(user)
        repeat = True
        while repeat:
            repeat = False
            for subject, action in perms:
                if subject in groups and not action.isupper() and action not in groups:
                    groups.add(action)
                    repeat = True

        return groups

class StartPoints(object):
    """Format and parse start points for search."""

    FORMAT_STRING = 'provider_start_point:%s'

    @classmethod
    def parse_args(cls, req_args, provider_list):
        """Return a dict of start points by provider from request args."""
        start_points = {}
        for provider in provider_list:
            start_points[provider.get_name()] = req_args.getfirst(
                cls.FORMAT_STRING % provider.get_name(),
                0
            )
        return start_points

    @classmethod
    def format(cls, results, prev_start_points):
        """Return dict of start_point name to value."""
        start_points = {}
        for result in results:
            backend_name = result['backend_name']
            if not backend_name in start_points:
                try:
                    prev_start = int(prev_start_points.get(backend_name, 0))
                except:
                    prev_start = 0
                start_points[backend_name] = prev_start
            start_points[backend_name] += 1
        
        return json.dumps(
            [
                {
                    'name': cls.FORMAT_STRING % name,
                    'value': value
                }
                for (name, value) in start_points.iteritems()
            ]
        )
