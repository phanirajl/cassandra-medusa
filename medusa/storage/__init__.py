# -*- coding: utf-8 -*-
# Copyright 2018 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import collections
import itertools
import logging
import operator
import pathlib
import re

from libcloud.storage.providers import Provider
from libcloud.common.types import InvalidCredsError
from retrying import retry

import medusa.index

from medusa.storage.cluster_backup import ClusterBackup
from medusa.storage.node_backup import NodeBackup
from medusa.storage.google_storage import GoogleStorage
from medusa.storage.local_storage import LocalStorage
from medusa.storage.s3_storage import S3Storage


ManifestObject = collections.namedtuple('ManifestObject', ['path', 'size', 'MD5'])

# pattern meant to match just the blob name, not the entire path
# the path is covered by the initial .*
# also retains extension if the name has any
INDEX_BLOB_NAME_PATTERN = re.compile('.*(tokenmap|schema|manifest|differential|incremental)_(.*)$')
INDEX_BLOB_WITH_TIMESTAMP_PATTERN = re.compile('.*(started|finished)_(.*)_([0-9]+).timestamp$')


def format_bytes_str(value):
    for unit_shift, unit in enumerate(['B', 'KB', 'MB', 'GB', 'TB']):
        if value >> (unit_shift * 10) < 1024:
            break
    return '{:.2f} {}'.format(value / (1 << (unit_shift * 10)), unit)


class Storage(object):
    def __init__(self, *, config):
        self._config = config
        self._prefix = pathlib.Path(config.prefix or '.')
        self.storage_driver = self._connect_storage()
        self.storage_provider = self._config.storage_provider

    def _connect_storage(self):
        if self._config.storage_provider == Provider.GOOGLE_STORAGE:
            return GoogleStorage(self._config)
        elif self._config.storage_provider.startswith(Provider.S3):
            return S3Storage(self._config)
        elif self._config.storage_provider == Provider.LOCAL:
            return LocalStorage(self._config)

        raise NotImplementedError("Unsupported storage provider")

    @property
    def config(self):
        return self._config

    @retry(stop_max_attempt_number=7, wait_exponential_multiplier=10000, wait_exponential_max=120000)
    def get_node_backup(self, *, fqdn, name, differential_mode=False):
        return NodeBackup(
            storage=self,
            name=name,
            fqdn=fqdn,
            differential_mode=differential_mode
        )

    def discover_node_backups(self, *, fqdn=None):
        """
        Discovers nodes backups by traversing data folders.
        This operation is very taxing for cloud backends and should be avoided.
        We keep it in the codebase for the sole reason of allowing the compute-backup-indices to work.
        """

        def get_backup_name_from_blob(blob):
            blob_path = pathlib.Path(blob.name)
            fqdn, name, *_ = blob_path.parts
            return fqdn, name

        def is_schema_blob(blob):
            return blob.name.endswith('/schema.cql')

        def includes_schema_blob(blobs):
            return any(map(is_schema_blob, blobs))

        prefix_path = fqdn if fqdn else ''

        logging.debug("Listing blobs with prefix '{}'".format(prefix_path))

        storage_objects = filter(
            lambda blob: "meta" in blob.name,
            self.storage_driver.list_objects(path=prefix_path)
        )

        all_blobs = sorted(storage_objects, key=operator.attrgetter('name'))

        logging.debug("Finished listing blobs")

        for (fqdn, backup_name), blobs in itertools.groupby(all_blobs, key=get_backup_name_from_blob):
            # consume the _blobs_ iterator into a list because we need to traverse it twice
            backup_blobs = list(blobs)
            if includes_schema_blob(backup_blobs):
                logging.debug("Found backup {}.{}".format(fqdn, backup_name))
                yield NodeBackup(storage=self, fqdn=fqdn, name=backup_name, preloaded_blobs=backup_blobs)

    def list_node_backups(self, *, fqdn=None, backup_index_blobs=None):
        """
        Lists node backups using the index.
        If there is no backup index, no backups will be found.
        Use discover_node_backups to discover backups from the data folders.
        """

        def is_tokenmap_file(blob):
            return "tokenmap" in blob.name

        def get_blob_name(blob):
            return blob.name

        def get_all_backup_blob_names(blobs):
            # if the tokenmap file exists, we assume the whole backup exists too
            all_backup_blobs = filter(is_tokenmap_file, blobs)
            return list(map(get_blob_name, all_backup_blobs))

        def get_blobs_for_fqdn(blobs, fqdn):
            return list(filter(lambda b: fqdn in b, blobs))

        if backup_index_blobs is None:
            backup_index_blobs = self.list_backup_index_blobs()

        blobs_by_backup = self.group_backup_index_by_backup_and_node(backup_index_blobs)

        all_backup_blob_names = get_all_backup_blob_names(backup_index_blobs)

        if len(all_backup_blob_names) == 0:
            logging.info('No backups found in index. Consider running "medusa build-index" if you have some backups')

        # possibly filter out backups only for given fqdn
        if fqdn is not None:
            relevant_backup_names = get_blobs_for_fqdn(all_backup_blob_names, fqdn)
        else:
            relevant_backup_names = all_backup_blob_names

        # use the backup names and fqdns from index entries to construct NodeBackup objects
        node_backups = list()
        for backup_index_entry in relevant_backup_names:
            _, _, backup_name, tokenmap_file = backup_index_entry.split('/')
            # tokenmap file is in format 'tokenmap_fqdn.json'
            tokenmap_fqdn = self.get_fqdn_from_any_index_blob(tokenmap_file)
            manifest_blob, schema_blob, tokenmap_blob = None, None, None
            started_blob, finished_blob = None, None
            started_timestamp, finished_timestamp = None, None
            if tokenmap_fqdn in blobs_by_backup[backup_name]:
                manifest_blob = self.lookup_blob(blobs_by_backup, backup_name, tokenmap_fqdn, 'manifest')
                schema_blob = self.lookup_blob(blobs_by_backup, backup_name, tokenmap_fqdn, 'schema')
                tokenmap_blob = self.lookup_blob(blobs_by_backup, backup_name, tokenmap_fqdn, 'tokenmap')
                started_blob = self.lookup_blob(blobs_by_backup, backup_name, tokenmap_fqdn, 'started')
                finished_blob = self.lookup_blob(blobs_by_backup, backup_name, tokenmap_fqdn, 'finished')
                differential_blob = self.lookup_blob(blobs_by_backup, backup_name, tokenmap_fqdn, 'differential')
                # Should be removed after while. Here for backwards compatibility.
                incremental_blob = self.lookup_blob(blobs_by_backup, backup_name, tokenmap_fqdn, 'incremental')
                if started_blob is not None:
                    started_timestamp = self.get_timestamp_from_blob_name(started_blob.name)
                else:
                    started_timestamp = None
                if finished_blob is not None:
                    finished_timestamp = self.get_timestamp_from_blob_name(finished_blob.name)
                else:
                    finished_timestamp = None

            nb = NodeBackup(storage=self, fqdn=tokenmap_fqdn, name=backup_name,
                            manifest_blob=manifest_blob, schema_blob=schema_blob, tokenmap_blob=tokenmap_blob,
                            started_timestamp=started_timestamp, started_blob=started_blob,
                            finished_timestamp=finished_timestamp, finished_blob=finished_blob,
                            differential_blob=differential_blob if differential_blob is not None else incremental_blob)
            node_backups.append(nb)

        # once we have all the backups, we sort them by their start time. we get oldest ones first
        sorted_node_backups = sorted(
            # before sorting the backups, ensure we can work out at least their start time
            filter(lambda nb: nb.started is not None, node_backups),
            key=lambda nb: nb.started
        )

        # then, before returning the backups, we pick only the existing ones
        previous_existed = False
        for node_backup in sorted_node_backups:

            # we try to be smart here - once we have seen an existing one, we assume all later ones exist too
            if previous_existed:
                yield node_backup
                continue

            # the idea is to save .exist() calls as they actually go to the storage backend and cost something
            # this is mostly meant to handle the transition period when backups expire before the index does,
            # which is a consequence of the transition period and running the build-index command

            if node_backup.exists():
                previous_existed = True
                yield node_backup
            else:
                logging.debug('Backup {} for fqdn {} present only in index'.format(node_backup.name, node_backup.fqdn))
                # if a backup doesn't exist, we should remove its entry from the index too
                try:
                    self.remove_backup_from_index(node_backup)
                except InvalidCredsError:
                    logging.debug(
                        'This account cannot perform the cleanup_storage'
                        '{} for fqdn {} present only in index.'
                        'Ignoring and continuing...'
                        .format(node_backup.name, node_backup.fqdn))

    def list_backup_index_blobs(self):
        path = 'index/backup_index'
        return self.storage_driver.list_objects(path)

    def group_backup_index_by_backup_and_node(self, backup_index_blobs):

        def get_backup_name(blob):
            return blob.name.split('/')[2]

        def name_and_fqdn(blob):
            return get_backup_name(blob), Storage.get_fqdn_from_any_index_blob(blob)

        def group_by_backup_name(blobs):
            return itertools.groupby(blobs, get_backup_name)

        def group_by_fqdn(blobs):
            return itertools.groupby(blobs, Storage.get_fqdn_from_any_index_blob)

        blobs_by_backup = {}
        sorted_backup_index_blobs = sorted(
            backup_index_blobs,
            key=name_and_fqdn
        )

        for backup_name, blobs in group_by_backup_name(sorted_backup_index_blobs):
            blobs_by_node = {}
            for fqdn, node_blobs in group_by_fqdn(blobs):
                blobs_by_node[fqdn] = list(node_blobs)
            blobs_by_backup[backup_name] = blobs_by_node

        return blobs_by_backup

    @staticmethod
    def get_fqdn_from_any_index_blob(blob):
        if not isinstance(blob, str):
            blob_name = blob.name
        else:
            blob_name = blob
        # it's important to check in this order, because the 2nd pattern is more generic
        match = INDEX_BLOB_WITH_TIMESTAMP_PATTERN.match(blob_name)
        if match is None:
            match = INDEX_BLOB_NAME_PATTERN.match(blob_name)
        assert match is not None, 'Encountered malformed index blob name {}'.format(blob_name)
        return Storage.remove_extension(match.group(2))

    @staticmethod
    def remove_extension(fqdn_with_extension):
        replaces = {
            '.json': '',
            '.cql': '',
            '.txt': '',
            '.timestamp': ''
        }
        r = fqdn_with_extension
        for old, new in replaces.items():
            r = r.replace(old, new)
        return r

    @staticmethod
    def get_timestamp_from_blob_name(blob_name):
        match = INDEX_BLOB_WITH_TIMESTAMP_PATTERN.match(blob_name)
        assert match is not None, 'Encountered malformed index blob name with timestamp {}'.format(blob_name)
        return int(match.group(3))

    def lookup_blob(self, blobs_by_backup, backup_name, fqdn, blob_name_chunk):
        """
        This function looks up blobs in blobs_by_backup, which is a double dict (k->k->v).
        The blob_name_chunk tells which blob for given backup and fqdn we want.
        It can be 'schema', 'manifest', 'started', 'finished'
        """
        blob_list = list(filter(lambda blob: blob_name_chunk in blob.name,
                                blobs_by_backup[backup_name][fqdn]))
        return blob_list[0] if len(blob_list) > 0 else None

    def list_cluster_backups(self, backup_index=None):
        node_backups = sorted(
            self.list_node_backups(backup_index_blobs=backup_index),
            key=lambda b: (b.name, b.started)
        )

        for name, node_backups in itertools.groupby(node_backups, key=operator.attrgetter('name')):
            yield ClusterBackup(name, node_backups)

    def latest_node_backup(self, *, fqdn):
        index_path = 'index/latest_backup/{}/backup_name.txt'.format(fqdn)
        try:
            latest_backup_name = self.storage_driver.get_blob_content_as_string(index_path)
            differential_blob = self.storage_driver.get_blob('{}/{}/meta/differential'.format(fqdn, latest_backup_name))
            # Should be removed after while. Here for backwards compatibility.
            incremental_blob = self.storage_driver.get_blob('{}/{}/meta/incremental'.format(fqdn, latest_backup_name))

            node_backup = NodeBackup(
                storage=self,
                fqdn=fqdn,
                name=latest_backup_name,
                differential_blob=differential_blob if differential_blob is not None else incremental_blob
            )

            if not node_backup.exists():
                logging.warning('Latest backup points to non-existent backup. Deleting the marker')
                self.remove_latest_backup_marker(fqdn)
                raise Exception

            return node_backup

        except Exception:
            logging.info('Node {} does not have latest backup'.format(fqdn))
            return None

    def latest_cluster_backup(self, backup_index=None):
        """
        Get the latest backup attempted (successful or not)
        """
        last_started = max(
            self.list_cluster_backups(backup_index=backup_index),
            key=operator.attrgetter('started'),
            default=None
        )

        logging.debug("Last cluster backup : {}".format(last_started))
        return last_started

    def latest_complete_cluster_backup(self, backup_index=None):
        """
        Get the latest *complete* backup (ie successful on all nodes)
        """
        finished_backups = filter(
            operator.attrgetter('finished'),
            self.list_cluster_backups(backup_index=backup_index)
        )

        last_finished = max(finished_backups, key=operator.attrgetter('finished'), default=None)
        return last_finished

    def get_cluster_backup(self, backup_name):
        for cluster_backup in self.list_cluster_backups():
            if cluster_backup.name == backup_name:
                return cluster_backup
        raise KeyError('No such backup')

    def remove_backup_from_index(self, node_backup):
        """
        Takes a node backup and tries to remove corresponding items from the index.
        This usually happens when the node_backup.exists() returns false, which means it's schema in the
        meta folder does not exist.
        We are checking and deleting each blob separately because there is no easy way to list and get the objects.
        """
        medusa.index.clean_backup_from_index(self, node_backup)

    def remove_latest_backup_marker(self, fqdn):
        """
        Removes the markers of the latest backup for a fqdn.
        Unlike remove_backup_from_index, here we do call list because the path is not ambiguous, and because we can't
        get the blobs from anywhere.
        Then we can call the delete object on the results.
        """
        markers = self.storage_driver.list_objects('index/latest_backup/{}/'.format(fqdn))
        for marker in markers:
            self.storage_driver.delete_object(marker)
