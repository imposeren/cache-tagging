# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals
import os
import hashlib
import random
import threading
import time
import warnings
from datetime import datetime, timedelta
from functools import wraps
from threading import local

try:
    import _thread
except ImportError:
    import thread as _thread  # Python < 3.*

try:
    str = unicode  # Python 2.* compatible
    string_types = (basestring,)
    integer_types = (int, long)
except NameError:
    string_types = (str,)
    integer_types = (int,)


__version__ = '0.7.7.0'

# Use the system (hardware-based) random number generator if it exists.
if hasattr(random, 'SystemRandom'):
    randrange = random.SystemRandom().randrange
else:
    randrange = random.randrange

TAG_TIMEOUT = 24 * 3600
TAG_LOCKING_TIMEOUT = 5
MAX_TAG_KEY = 18446744073709551616     # 2 << 63


def warn(old, new, stacklevel=3):
    warnings.warn(
        "{0} is deprecated. Use {1} instead".format(old, new),
        PendingDeprecationWarning,
        stacklevel=stacklevel
    )


class CacheTagging(object):
    """Tags support for Django cache."""

    def __init__(self, cache, delay=None, nonrepeatable_reads=False):
        """Constructor of cache instance."""
        self.cache = cache
        self.ignore_descendants = False
        self.ctx = local()
        self.transaction = Transaction(self, delay, nonrepeatable_reads)

    def get_or_set_callback(self, name, callback, tags=[], timeout=None,
                            version=None, args=None, kwargs=None):
        """Returns cache value if exists

        Otherwise calls cache_funcs, sets cache value to it and returns it.
        """
        value = self.get(name, version=version)
        if value is None:
            args = args or []
            kwargs = kwargs or {}
            value = callback(*args, **kwargs)
            self.set(name, value, tags, timeout, version)
        return value

    def get(self, name, default=None, version=None, abort=False):
        """Gets cache value.

        If one of cache tags is expired, returns default.
        """
        if not abort and not self.ignore_descendants:
            self.begin(name)
        data = self.cache.get(name, None, version)
        if data is None:
            return default

        if not isinstance(data, dict) or 'tag_versions' not in data\
                or 'value' not in data:
            return data  # Returns native API

        if len(data['tag_versions']):
            tag_caches = self.cache.get_many(
                list(map(tag_prepare_name, list(data['tag_versions'].keys()))),
                version
            )
            for tag, tag_version in data['tag_versions'].items():
                tag_prepared = tag_prepare_name(tag)
                if tag_prepared not in tag_caches\
                        or tag_caches[tag_prepared] != tag_version:
                    return default

        self.finish(name, data['tag_versions'].keys(), version=version)
        return data['value']

    def set(self, name, value, tags=(), timeout=None, version=None):
        """Sets cache value and tags."""
        if not hasattr(tags, '__iter__'):  # Called as native API
            if timeout is not None and version is None:
                version = timeout
            timeout = tags
            return self.cache.set(name, value, timeout, version)

        tags = set(tags)
        # pull tags from descendants (cached fragments)
        try:
            tags.update(self.ancestors[name][version])
        except KeyError:
            pass

        tag_versions = {}
        if len(tags):
            tag_cache_names = list(map(tag_prepare_name, tags))
            if self.transaction.is_locked_any_tag(tag_cache_names, version):
                self.finish(name, tags, version=version)
                return
            tag_caches = self.cache.get_many(
                tag_cache_names
            ) or {}
            tag_new_dict = {}
            for tag in tags:
                tag_prepared = tag_prepare_name(tag)
                if tag_prepared not in tag_caches\
                        or tag_caches[tag_prepared] is None:
                    tag_version = tag_generate_version()
                    tag_new_dict[tag_prepared] = tag_version
                else:
                    tag_version = tag_caches[tag_prepared]
                tag_versions[tag] = tag_version
            if len(tag_new_dict):
                self.cache.set_many(tag_new_dict, TAG_TIMEOUT, version)

        data = {
            'tag_versions': tag_versions,
            'value': value,
        }

        self.finish(name, tags, version=version)
        return self.cache.set(name, data, timeout, version)

    def invalidate_tags(self, *tags, **kwargs):
        """Invalidate specified tags"""
        if len(tags):
            version = kwargs.get('version', None)
            tags_prepared = list(map(tag_prepare_name, set(tags)))
            self.transaction.add_tags(tags_prepared, version=version)
            self.cache.delete_many(tags_prepared, version=version)

    @property
    def ancestors(self):
        """Returns ancestors dict."""
        if not hasattr(self.ctx, 'ancestors'):
            self.ctx.ancestors = {}
        return self.ctx.ancestors

    def add_tags_to_ancestors(self, tags, version=None):
        """add tags to ancestors"""
        for cachename, versions in self.ancestors.items():
            versions.setdefault(version, set()).update(tags)

    def begin(self, name):
        """Start cache creating."""
        self.ancestors[name] = {}

    def abort(self, name):
        """Clean tags for given cache name."""
        self.ancestors.pop(name, {})

    def finish(self, name, tags, version=None):
        """Start cache creating."""
        self.ancestors.pop(name, {})
        self.add_tags_to_ancestors(tags, version=version)

    def transaction_begin(self):
        warn('cache.transaction_begin()', 'cache.transaction.begin()')
        self.transaction.begin()
        return self

    def transaction_finish(self):
        warn('cache.transaction_finish()', 'cache.transaction.finish()')
        self.transaction.finish()
        return self

    def transaction_finish_all(self):
        warn('cache.transaction_finish_all()', 'cache.transaction.flush()')
        self.transaction.flush()
        return self

    def get_transaction_scopes(self):
        warn('cache.get_transaction_scope()', 'cache.transaction.scopes')
        return self.transaction.scopes

    def add_to_transaction_scope(self, tags, version=None):
        warn('cache.add_to_transaction_scope()', 'cache.transaction.add_tags()')
        self.transaction.add_tags(tags, version)

    def __getattr__(self, name):
        """Proxy for all native methods."""
        return getattr(self.cache, name)


def tag_prepare_name(name):
    """Adds prefixed namespace for tag name"""
    version = str(__version__).replace('.', '')
    name = hashlib.md5(str(name).encode('utf-8')).hexdigest()
    return 'tag_{0}_{1}'.format(version, name)


def tag_generate_version():
    """ Generates a new unique identifier for tag version."""
    pid = os.getpid()
    tid = _thread.get_ident()
    hash = hashlib.md5("{0}{1}{2}{3}".format(
        randrange(0, MAX_TAG_KEY),
        pid,
        tid,
        time.time()
    )).hexdigest()
    return hash


class Transaction(object):

    def __init__(self, cache, delay=None, nonrepeatable_reads=False):
        """Constructor of Transaction instance."""
        self.cache = cache
        self.delay = delay
        self.nonrepeatable_reads = nonrepeatable_reads
        self.ctx = local()

    def __call__(self, f=None):
        if f is None:
            return self

        @wraps(f)
        def _decorated(*args, **kw):
            with self:
                rv = f(*args, **kw)
            return rv

        return _decorated

    def __enter__(self):
        self.begin()

    def __exit__(self, *args):
        self.finish()
        return False

    def get_locked_tag_name(self, tag):
        return 'lock_{0}'.format(tag)

    def lock_tags(self, tags, version=None):
        """Locks tags for concurrent transactions."""
        date = datetime.now()
        timeout = TAG_LOCKING_TIMEOUT
        if self.delay:
            timeout += self.delay
        self.cache.set_many(
            {self.get_locked_tag_name(tag): date for tag in tags},
            timeout,
            version
        )

    def is_locked_any_tag(self, tags, version=None):
        """Returns True, if current transaction has been started earlier than

        any tag has been invalidated by concurent process.
        Actual for SERIALIZABLE and REPEATABLE READ transaction levels.
        """
        if self.nonrepeatable_reads and self.scopes:
            top_scope = self.scopes[0]
            thread_tags = top_scope['tags'].get(version, set())
            concurrent_tags = list(set(tags) - thread_tags)
            if concurrent_tags:
                locked_tags = self.cache.get_many(
                    list(map(self.get_locked_tag_name, concurrent_tags))
                ) or {}
                transaction_start_date = top_scope['date']
                if self.delay:
                    transaction_start_date -= timedelta(seconds=self.delay)
                for tag_date in locked_tags.values():
                    if transaction_start_date <= tag_date:
                        return True
        return False

    def begin(self):
        """Handles database transaction begin."""
        self.scopes.append({'date': datetime.now(), 'tags': {}})
        return self

    def _finish_delayed(self, scope):
        """Just helper for async. Actual for DB replication (slave delay)."""
        for version, tags in scope['tags'].items():
            self.cache.delete_many(list(tags), version=version)
            self.lock_tags(tags, version)

    def finish(self):
        """Handles database transaction commit or rollback.

        In any case (commit or rollback) we need to invalidate tags,
        because caches can be generated for
        current database session (for rollback case) or
        another database session (for commit case).
        So, method is named "finish" (not "commit"
        or "rollback").
        """
        scope = self.scopes.pop()
        self._finish_delayed(scope)
        if self.delay and not self.nonrepeatable_reads:
            threading.Timer(self.delay, self._finish_delayed, [scope]).start()
        return self

    def flush(self):
        """Finishes all active transactions."""
        while len(self.scopes):
            self.finish()
        return self

    @property
    def scopes(self):
        """Get transaction scopes."""
        if not hasattr(self.ctx, 'transaction_scopes'):
            self.ctx.transaction_scopes = []
        return self.ctx.transaction_scopes

    def add_tags(self, tags, version=None):
        """Adds cache names to current scope."""
        for scope in self.scopes:
            scope['tags'].setdefault(version, set()).update(tags)
            self.lock_tags(tags, version)
