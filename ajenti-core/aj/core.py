from __future__ import unicode_literals

import locale
import logging
import os
import psutil
import signal
import socket
import sys
import syslog
from jadi import Context
from importlib import reload

import aj
import aj.plugins
# from aj.auth import AuthenticationService # Test for callback with certificate
from aj.config import AjentiUsers
from aj.http import HttpRoot, HttpMiddlewareAggregator
from aj.gate.middleware import GateMiddleware
from aj.plugins import PluginManager
from aj.wsgi import RequestHandler

import gevent
import ssl
import gevent.ssl
from gevent import monkey

# Gevent monkeypatch ---------------------
monkey.patch_all(select=True, thread=True, aggressive=False, subprocess=True)

from gevent.event import Event
import threading
threading.Event = Event
# ----------------------------------------

import aj.compat

from socketio.server import SocketIOServer


def run(config=None, plugin_providers=None, product_name='ajenti', dev_mode=False,
        debug_mode=False, autologin=False):
    """
    A global entry point for Ajenti.

    :param config: config file implementation instance to use
    :type  config: :class:`aj.config.BaseConfig`
    :param plugin_providers: list of plugin providers to load plugins from
    :type  plugin_providers: list(:class:`aj.plugins.PluginProvider`)
    :param str product_name: a product name to use
    :param bool dev_mode: enables dev mode (automatic resource recompilation)
    :param bool debug_mode: enables debug mode (verbose and extra logging)
    :param bool autologin: disables authentication and logs everyone in as the user running the panel. This is EXTREMELY INSECURE.
    """
    if config is None:
        raise TypeError('`config` can\'t be None')

    reload(sys)
    if hasattr(sys, 'setdefaultencoding'):
        sys.setdefaultencoding('utf8')

    aj.product = product_name
    aj.debug = debug_mode
    aj.dev = dev_mode
    aj.dev_autologin = autologin

    aj.init()
    aj.log.set_log_params(tag='master', master_pid=os.getpid())
    aj.context = Context()
    aj.config = config
    aj.plugin_providers = plugin_providers or []
    logging.info('Loading config from %s', aj.config)
    aj.config.load()
    aj.config.ensure_structure()

    if not aj.config.data['auth']['secret']:
        logging.info('No secret found, generating new and writing it in config.yml')
        aj.config.data['auth']['secret'] = os.urandom(16).hex()
        aj.config.save()

    logging.info('Loading users from %s', '/etc/ajenti/users.yml')
    aj.users = AjentiUsers(aj.config.data['auth']['users_file'])
    aj.users.load()

    if aj.debug:
        logging.warning('Debug mode')
    if aj.dev:
        logging.warning('Dev mode')

    try:
        locale.setlocale(locale.LC_ALL, '')
    except locale.Error:
        logging.warning('Couldn\'t set default locale')

    # install a passthrough gettext replacement since all localization is handled in frontend
    # and _() is here only for string extraction
    __builtins__['_'] = lambda x: x

    logging.info('Ajenti Core %s', aj.version)
    logging.info('Master PID - %s', os.getpid())
    logging.info('Detected platform: %s / %s', aj.platform, aj.platform_string)
    logging.info('Python version: %s', aj.python_version)

    # Load plugins
    PluginManager.get(aj.context).load_all_from(aj.plugin_providers)
    if len(PluginManager.get(aj.context)) == 0:
        logging.warning('No plugins were loaded!')

    if aj.config.data['bind']['mode'] == 'unix':
        path = aj.config.data['bind']['socket']
        if os.path.exists(path):
            os.unlink(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(path)
        except OSError:
            logging.error('Could not bind to %s', path)
            sys.exit(1)

    if aj.config.data['bind']['mode'] == 'tcp':
        host = aj.config.data['bind']['host']
        port = aj.config.data['bind']['port']
        listener = socket.socket(
            socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_STREAM
        )
        if aj.platform not in ['freebsd', 'osx']:
            try:
                listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_CORK, 1)
            except socket.error:
                logging.warning('Could not set TCP_CORK')
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        logging.info('Binding to [%s]:%s', host, port)
        try:
            listener.bind((host, port))
        except socket.error as e:
            logging.error('Could not bind: %s', str(e))
            sys.exit(1)

    # Fix stupid socketio bug (it tries to do *args[0][0])
    socket.socket.__getitem__ = lambda x, y: None

    listener.listen(10)

    gateway = GateMiddleware.get(aj.context)
    application = HttpRoot(HttpMiddlewareAggregator([gateway])).dispatch

    aj.server = SocketIOServer(
        listener,
        log=open(os.devnull, 'w'),
        application=application,
        handler_class=RequestHandler,
        policy_server=False,
        transports=[
            str('websocket'),
            str('xhr-polling'),
            str('jsonp-polling'),
        ],
    )

    if aj.config.data['ssl']['enable'] and aj.config.data['bind']['mode'] == 'tcp':
        aj.server.ssl_args = {'server_side': True}
        cert_path = aj.config.data['ssl']['certificate']
        if aj.config.data['ssl']['fqdn_certificate']:
            fqdn_cert_path = aj.config.data['ssl']['fqdn_certificate']
        else:
            fqdn_cert_path = cert_path

        context = gevent.ssl.SSLContext(ssl.PROTOCOL_TLS)
        context.load_cert_chain(certfile=fqdn_cert_path, keyfile=fqdn_cert_path)
        context.options |= ssl.OP_NO_SSLv2 | ssl.OP_NO_SSLv3 | ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
        context.set_ciphers('ALL:!ADH:!EXP:!LOW:!RC2:!3DES:!SEED:!RC4:+HIGH:+MEDIUM')

        if aj.config.data['ssl']['client_auth']['enable']:

            logging.info('Enabling SSL client authentication')
            context.load_verify_locations(cafile=cert_path)
            if aj.config.data['ssl']['client_auth']['force']:
                context.verify_mode = ssl.CERT_REQUIRED
            else:
                context.verify_mode = ssl.CERT_OPTIONAL

            ## Test callback : client_certificate_callback must return None to get forward
            # context.set_servername_callback(AuthenticationService.get(aj.context).client_certificate_callback)

        aj.server.wrap_socket = lambda socket, **args:context.wrap_socket(sock=socket, server_side=True)
        logging.info('SSL enabled')

    # auth.log
    try:
        syslog.openlog(
            ident=str(aj.product),
            facility=syslog.LOG_AUTH,
        )
    except Exception as e:
        syslog.openlog(aj.product)

    def cleanup():
        if hasattr(cleanup, 'started'):
            return
        cleanup.started = True
        logging.info('Process %s exiting normally', os.getpid())
        gevent.signal_handler(signal.SIGINT, lambda: None)
        gevent.signal_handler(signal.SIGTERM, lambda: None)
        if aj.master:
            gateway.destroy()

        p = psutil.Process(os.getpid())
        for c in p.children(recursive=True):
            try:
                os.killpg(c.pid, signal.SIGTERM)
                os.killpg(c.pid, signal.SIGKILL)
            except OSError:
                pass

    def signal_handler():
        cleanup()
        sys.exit(0)

    gevent.signal_handler(signal.SIGINT, signal_handler)
    gevent.signal_handler(signal.SIGTERM, signal_handler)

    aj.server.serve_forever()

    if not aj.master:
        # child process, server is stopped, wait until killed
        gevent.wait()

    if hasattr(aj.server, 'restart_marker'):
        logging.warning('Restarting by request')
        cleanup()

        fd = 20  # Close all descriptors. Creepy thing
        while fd > 2:
            try:
                os.close(fd)
                logging.debug('Closed descriptor #%i', fd)
            except OSError:
                pass
            fd -= 1

        logging.warning('Will restart the process now')
        if '-d' in sys.argv:
            sys.argv.remove('-d')
        os.execv(sys.argv[0], sys.argv)
    else:
        if aj.master:
            logging.debug('Server stopped')
            cleanup()
