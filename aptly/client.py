# -*- coding: utf-8 -*-

import requests
import json
import time
import re
import logging

lg = logging.getLogger(__name__)


class Aptly(object):
    def __init__(self, url, auth=None, timeout=60, dry=False):
        self.url = '%s%s' % (url, '/api')
        self.timeout = timeout
        self.dry = dry

        self.session = requests.Session()
        if auth is not None:
            self.session.auth = auth
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-type': 'application/json',
        })

    def _process_result(self, res):
        if res.status_code < 200 or res.status_code >= 300:
            raise AptlyException(
                res,
                "Something went wrong: %s (%s)" % (res.reason, res.status_code)
            )
        try:
            return res.json()
        except ValueError:
            return res.text

    def do_get(self, uri, kwargs=None, timeout=None):
        url = '%s%s' % (self.url, uri)
        lg.debug("GET %s, args=%s" % (url, kwargs))
        res = self.session.get(
            url,
            timeout=timeout or self.timeout,
            params=kwargs,
        )
        return self._process_result(res)

    def do_post(self, uri, data, timeout=None):
        data_json = json.dumps(data)
        url = '%s%s' % (self.url, uri)
        lg.debug("POST %s, data=%s" % (url, data_json))

        if self.dry:
            return

        res = self.session.post(
            url,
            timeout=timeout or self.timeout,
            data=data_json,
        )
        return self._process_result(res)

    def do_delete(self, uri, timeout=None):
        url = '%s%s' % (self.url, uri)
        lg.debug("DELETE %s" % url)

        if self.dry:
            return

        res = self.session.delete(
            url,
            timeout=timeout or self.timeout,
        )
        return self._process_result(res)

    def do_put(self, uri, data, timeout=None):
        data_json = json.dumps(data)
        url = '%s%s' % (self.url, uri)
        lg.debug("PUT %s, data=%s" % (url, data_json))

        if self.dry:
            return

        res = self.session.put(
            url,
            timeout=timeout or self.timeout,
            data=data_json,
        )
        return self._process_result(res)


class Publish(object):
    def __init__(self, client):
        self.client = client
        self.components = {}
        self.distributions = {}
        self.publish_snapshots = []

    def add(self, name, distributions, snapshot, component='main'):
        try:
            self.components[component].append(snapshot)
        except KeyError:
            self.components[component] = [snapshot]

        for distribution in distributions:
            try:
                self.distributions[distribution].append(self.components[component])
            except KeyError:
                self.distributions[distribution] = [self.components[component]]

    def merge_snapshots(self):
        for component, snapshots in self.components.iteritems():
            if len(snapshots) <= 1:
                # Only one snapshot, no need to merge
                lg.debug("Component %s has only one snapshot %s, not creating merge snapshot" % (component, snapshots))
                self.publish_snapshots.append({
                    'Component': component,
                    'Name': snapshots[0]
                })
                continue

            # Look if merged snapshot doesn't already exist
            remote_snapshots = self.client.do_get('/snapshots', {'sort': 'time'})
            for remote in reversed(remote_snapshots):
                if remote['Name'].startswith('%s-' % component):
                    remote_snapshot = remote
                    break

            # Check if latest merged snapshot has same source snapshots like us
            # Unfortunately we have to decide by description
            source_snapshots = re.findall(r"'([\w\d-]+)'", remote_snapshot['Description'])
            snapshots_want = list(snapshots)
            snapshots_want.sort()
            source_snapshots.sort()

            if snapshots_want == source_snapshots:
                lg.info("Remote merge snapshot already exists: %s (%s)" % (remote_snapshot['Name'], source_snapshots))
                self.publish_snapshots.append({
                    'Component': component,
                    'Name': remote_snapshot['Name']
                })
                continue

            snapshot_name = '%s-%s' % (component, int(time.time()))
            lg.info("Creating merge snapshot %s for component %s of snapshots %s" % (snapshot_name, component, snapshots))
            package_refs = []
            for snapshot in snapshots:
                # Get package refs from each snapshot
                packages = self.client.do_get('/snapshots/%s/packages' % snapshot)
                package_refs.extend(packages)

            self.client.do_post(
                '/snapshots',
                data={
                    'Name': snapshot_name,
                    'SourceSnapshots': snapshots,
                    'Description': "Merged from sources: %s" % ', '.join("'%s'" % snap for snap in snapshots),
                    'PackageRefs': package_refs,
                }
            )

            self.publish_snapshots.append({
                'Component': component,
                'Name': snapshot_name
            })

    def update_publish(self, distribution):
        # TODO: publish_snapshots should be constructed per-distribution
        dist_split = distribution.split('/')
        name = dist_split[-1]
        if dist_split[0] != name:
            prefix = dist_split[0]
        else:
            prefix = ''

        self.client.do_put(
            '/publish/%s/%s' % (prefix, name),
            {'Snapshots': self.publish_snapshots}
        )

    def create_publish(self, distribution):
        # TODO: publish_snapshots should be constructed per-distribution
        raise NotImplemented("Creation of new publish is not implemented")

    def cleanup_snapshots(self):
        exclude = []

        # Add currently published snapshots into exclude list
        publishes = self.client.do_get('/publish')
        for publish in publishes:
            exclude.extend(
                [x['Name'] for x in publish['Sources']]
            )

        # Add last snapshots into exclude list
        for component, snapshots in self.components.iteritems():
            exclude.extend(snapshots)

        exclude = self.list_uniq(exclude)

        snapshots = self.client.do_get('/snapshots', {'sort': 'time'})
        for snapshot in snapshots:
            if snapshot['Name'] not in exclude:
                lg.info("Deleting snapshot %s" % snapshot['Name'])
                try:
                    self.client.do_delete('/snapshots/%s' % snapshot['Name'])
                except AptlyException as e:
                    if e.res.status_code == 409:
                        lg.warning("Snapshot %s is being used, can't delete" % snapshot['Name'])
                    else:
                        raise

    def list_uniq(self, seq):
        keys = {}
        for e in seq:
            keys[e] = 1
        return keys.keys()

    def do_publish(self):
        publishes = self.client.do_get('/publish')
        for distribution in self.distributions.iterkeys():
            dist_split = distribution.split('/')
            name = dist_split[-1]
            if dist_split[0] != name:
                prefix = dist_split[0]
            else:
                prefix = '.'

            match = False
            for publish in publishes:
                if publish['Distribution'] == name \
                        and publish['Prefix'] == prefix:
                    # Update publish
                    match = True
                    to_publish = [x['Name'] for x in self.publish_snapshots]
                    published = [x['Name'] for x in publish['Sources']]

                    to_publish.sort()
                    published.sort()

                    if to_publish == published:
                        lg.info("Publish %s is up to date" % name)
                    else:
                        self.update_publish(distribution)

            if not match:
                # New publish
                self.create_publish(distribution)


class AptlyException(Exception):
    def __init__(self, res, msg):
        Exception.__init__(self, msg)
        self.res = res
