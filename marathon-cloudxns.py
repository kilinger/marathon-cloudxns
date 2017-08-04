#!/usr/bin/env python2
# -*- coding:utf-8 -*-

"""
Usage:
  $ servicerouter.py --marathon http://marathon1:8080
"""

from logging.handlers import SysLogHandler
from operator import attrgetter
from wsgiref.simple_server import make_server
from cloudxns.api import Api

import argparse
import json
import logging
import os
import os.path
import re
import requests
import sys
import time


def string_to_bool(s):
    return s.lower() in ["true", "t", "yes", "y"]


logger = logging.getLogger('marathon-cloudxns')


class MarathonApp(object):

    def __init__(self, marathon, appId, app):
        self.app = app
        self.groups = frozenset()
        self.appId = appId

        self.mode = "tcp"

    def __hash__(self):
        return hash(self.appId)

    def __eq__(self, other):
        return self.appId == other.appId


class Cloudxns(object):

    def __init__(self, domain, hosts, api_key, secret_key):
        self.domain = str(domain)
        self.hosts = hosts.split(",")
        self.api_key = api_key
        self.secret_key = secret_key
        self.api = Api(api_key=self.api_key, secret_key=self.secret_key)
        self.domain_id = self.get_domain_id()

    def parse_response(self, response):
        response = json.loads(response)
        if response['message'] == 'success':
            return response
        else:
            logger.error(response['message'])

    def get_domain_id(self):
        response = self.parse_response(self.api.domain_list())
        if response:
            for data in response['data']:
                if data['domain'] == self.domain:
                    return data['id']
            response = self.parse_response(self.api.domain_add(self.domain[:-1]))
            if response:
                return response['id']

    def update(self, subdomain):

        domain_id = int(self.domain_id)
        host_name = str(subdomain)

        for host in self.hosts:
            self.parse_response(self.api.record_add(domain_id=domain_id, host_name=host_name, value=host))

        result = self.parse_response(self.api.host_list(domain_id=domain_id))
        if result['message'] == 'success':
            for host in result['hosts']:
                if host['host'] == host_name:
                    result = self.parse_response(self.api.record_list(domain_id, host_id=host['id']))
                    data = result['data']
                    for datum in data:
                        if datum['value'] in self.hosts:
                            continue
                        else:
                            self.parse_response(self.api.record_delete(domain_id=domain_id,
                                                                       record_id=datum['record_id']))


class Marathon(object):

    def __init__(self, hosts):
        # TODO(cmaloney): Support getting master list from zookeeper
        self.__hosts = hosts

    def api_req_raw(self, method, path, body=None, **kwargs):
        for host in self.__hosts:
            path_str = os.path.join(host, 'v2')

            for path_elem in path:
                path_str = path_str + "/" + path_elem
            response = requests.request(
                method,
                path_str,
                headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                **kwargs
            )

            logger.debug("%s %s", method, response.url)
            if response.status_code == 200:
                break
        if 'message' in response.json():
            response.reason = "%s (%s)" % (
                response.reason,
                response.json()['message'])
        response.raise_for_status()
        return response

    def api_req(self, method, path, **kwargs):
        return self.api_req_raw(method, path, **kwargs).json()

    def create(self, app_json):
        return self.api_req('POST', ['apps'], **app_json)

    def get_app(self, appid):
        logger.info('fetching app %s', appid)
        return self.api_req('GET', ['apps', appid])["app"]

    # Lists all running apps.
    def list(self):
        logger.info('fetching apps')
        return self.api_req('GET', ['apps'])["apps"]

    def tasks(self):
        logger.info('fetching tasks')
        return self.api_req('GET', ['tasks'])["tasks"]

    def add_subscriber(self, callbackUrl):
        return self.api_req('POST',
                            ['eventSubscriptions'],
                            params={'callbackUrl': callbackUrl})

    def remove_subscriber(self, callbackUrl):
        return self.api_req('DELETE',
                            ['eventSubscriptions'],
                            params={'callbackUrl': callbackUrl})

    def get_event_stream(self):
        url = self.__hosts[0] + "/v2/events"
        logger.info(
            "SSE Active, trying fetch events from from {0}".format(url))
        response = requests.get(url, stream=True, headers={'Cache-Control': 'no-cache',
                                                           'Accept': 'text/event-stream'})
        return response.iter_lines()


def has_group(groups, app_groups):
    # All groups / wildcard match
    if '*' in groups:
        return True

    # empty group only
    if len(groups) == 0 and len(app_groups) == 0:
        return True

    # Contains matching groups
    if (len(frozenset(app_groups) & groups)):
        return True

    return False


def regenerate_dns(apps, groups, cloudxns):
    logger.info("generating config")
    groups = frozenset(groups)

    for app in sorted(apps, key=attrgetter('appId', )):
        # App only applies if we have it's group
        if not has_group(groups, app.groups):
            continue

        if app.mode != 'http':
            continue

        logger.debug("configuring app %s", app.appId)
        if app.appId[1:].find("/"):
            marathon_id_list = app.appId[1:].split("/")
            marathon_id_list.remove("apps") if "apps" in marathon_id_list else None
            marathon_id_list.remove("addons") if "addons" in marathon_id_list else None
            marathon_id_list.reverse()
            subdomain = "-".join(marathon_id_list)
        else:
            subdomain = app.appId[1:]
        cloudxns.update(subdomain)


def get_apps(marathon):
    apps = marathon.list()
    logger.debug("got apps %s", map(lambda app: app["id"], apps))

    marathon_apps = []
    for app in apps:
        appId = app['id']
        marathon_app = MarathonApp(marathon, appId, app)

        if 'HAPROXY_GROUP' in marathon_app.app['labels']:
            marathon_app.groups = \
                marathon_app.app['labels']['HAPROXY_GROUP'].split(',')
        if 'HAPROXY_0_VHOST' in marathon_app.app['labels']:
            marathon_app.mode = 'http'

        marathon_apps.append(marathon_app)

    return marathon_apps


class MarathonEventProcessor(object):

    def __init__(self, marathon, groups, cloudxns):
        self.__marathon = marathon
        # appId -> MarathonApp
        self.__apps = dict()
        self.__groups = groups
        self.__cloudxns = cloudxns

        # Fetch the base data
        self.reset_from_tasks()

    def reset_from_tasks(self):
        start_time = time.time()

        self.__apps = get_apps(self.__marathon)
        regenerate_dns(self.__apps,
                       self.__groups,
                       self.__cloudxns)

        logger.debug("updating tasks finished, took %s seconds",
                     time.time() - start_time)

    def handle_event(self, event):
        if event['eventType'] == 'status_update_event':
            # TODO (cmaloney): Handle events more intelligently so we don't
            # unnecessarily hammer the Marathon API.
            self.reset_from_tasks()


def get_arg_parser():
    parser = argparse.ArgumentParser(
        description="Marathon HAProxy Service Router")
    parser.add_argument("--longhelp",
                        help="Print out configuration details",
                        action="store_true"
                        )
    parser.add_argument("--marathon", "-m",
                        nargs="+",
                        help="Marathon endpoint, eg. -m " +
                             "http://marathon1:8080 -m http://marathon2:8080"
                        )
    parser.add_argument("--listening", "-l",
                        help="The address this script listens on for marathon" +
                             "events"
                        )
    parser.add_argument("--callback-url", "-u",
                        help="The HTTP address that Marathon can call this " +
                             "script back at (http://lb1:8080)"
                        )
    default_log_socket = "/dev/log"
    if sys.platform == "darwin":
        default_log_socket = "/var/run/syslog"

    parser.add_argument("--syslog-socket",
                        help="Socket to write syslog messages to. "
                        "Use '/dev/null' to disable logging to syslog",
                        default=default_log_socket
                        )
    parser.add_argument("--log-format",
                        help="Set log message format",
                        default="%(name)s: %(message)s"
                        )
    parser.add_argument("--cloudxns-api-key",
                        help="Cloudxns API key",
                        )
    parser.add_argument("--cloudxns-secret-key",
                        help="Cloudxns secret key",
                        )
    parser.add_argument("--cloudxns-domain",
                        help="Cloudxns domain",
                        default="cloud"
                        )
    parser.add_argument("--hosts",
                        help="LBS hosts",
                        default="127.0.0.1"
                        )
    parser.add_argument("--group",
                        help="Only generate config for apps which list the "
                        "specified names. Defaults to apps without groups. "
                        "Use '*' to match all groups",
                        action="append",
                        default=list())
    parser.add_argument("--sse", "-s",
                        help="Use Server Sent Events instead of HTTP "
                        "Callbacks",
                        action="store_true")

    return parser


def run_server(marathon, listen_addr, callback_url, config_file, groups):
    subscriber = MarathonEventSubscriber(marathon,
                                         callback_url,
                                         config_file,
                                         groups)
    marathon.add_subscriber(callback_url)

    # TODO(cmaloney): Switch to a sane http server
    # TODO(cmaloney): Good exception catching, etc
    def wsgi_app(env, start_response):
        length = int(env['CONTENT_LENGTH'])
        data = env['wsgi.input'].read(length)
        processor.handle_event(json.loads(data))
        # TODO(cmaloney): Make this have a simple useful webui for debugging /
        # monitoring
        start_response('200 OK', [('Content-Type', 'text/html')])

        return "Got it\n"

    httpd = make_server(listen_addr.split(':'), wsgi_app)
    httpd.serve_forever()


def clear_callbacks(marathon, callback_url):
    logger.info("Cleanup, removing subscription to {0}".format(callback_url))
    marathon.remove_subscriber(callback_url)


def process_sse_events(marathon, groups, cloudxns):
    processor = MarathonEventProcessor(marathon, groups, cloudxns)
    events = marathon.get_event_stream()
    for event in events:
        try:
            # logger.info("received event: {0}".format(event))
            # marathon might also send empty messages as keepalive...
            if event.strip() != '':
                # marathon sometimes sends more than one json per event
                # e.g. {}\r\n{}\r\n\r\n
                for real_event_data in re.split(r'\r\n', event):
                    if real_event_data[:6] == "data: ":
                        data = json.loads(real_event_data[6:])
                        logger.info(
                            "received event of type {0}".format(data['eventType']))
                        processor.handle_event(data)
            else:
                logger.info("skipping empty message")
        except:
            print event
            print "Unexpected error:", sys.exc_info()[0]
            raise


def setup_logging(syslog_socket, log_format):
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(log_format)

    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(formatter)
    logger.addHandler(consoleHandler)

    if args.syslog_socket != '/dev/null':
        syslogHandler = SysLogHandler(args.syslog_socket)
        syslogHandler.setFormatter(formatter)
        logger.addHandler(syslogHandler)

if __name__ == '__main__':
    # Process arguments
    arg_parser = get_arg_parser()
    args = arg_parser.parse_args()

    # Print the long help text if flag is set
    if args.longhelp:
        print __doc__
        sys.exit()
    # otherwise make sure that a Marathon URL was specified
    else:
        if args.marathon is None:
            arg_parser.error('argument --marathon/-m is required')
        if args.sse and args.listening:
            arg_parser.error(
                'cannot use --listening and --sse at the same time')

    # Setup logging
    setup_logging(args.syslog_socket, args.log_format)

    # Marathon API connector
    marathon = Marathon(args.marathon)

    # cloudxns API
    cloudxns = Cloudxns(args.cloudxns_domain, args.hosts, args.cloudxns_api_key, args.cloudxns_secret_key)

    # If in listening mode, spawn a webserver waiting for events. Otherwise
    # just write the config.
    if args.listening:
        callback_url = args.callback_url or args.listening
        try:
            run_server(marathon, args.listening, callback_url,
                       args.group, cloudxns)
        finally:
            clear_callbacks(marathon, callback_url)
    elif args.sse:
        while True:
            try:
                process_sse_events(marathon, args.group, cloudxns)
            except:
                logger.exception("Caught exception")
                logger.error("Reconnecting...")
            time.sleep(1)
    else:
        # Generate base config
        regenerate_dns(get_apps(marathon), args.group, cloudxns)
