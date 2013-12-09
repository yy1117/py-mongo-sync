#!/usr/bin/env python

# filename: mongosync.py
# summary: mongo synchronize tool
# author: caosiyang
# date: 2013/09/16

import os
import sys
import types
import time
import shutil
import argparse
from pymongo import MongoClient
from pymongo.database import Database
from utils import *
from mongo_sync_utils import *
from bson.timestamp import Timestamp
import settings

class MongoSynchronizer:
    """Mongodb synchronizer."""
    def __init__(self, src_host=None, src_port=None, dst_host=None, dst_port=None, dbs=[], **kwargs):
        """Constructor."""
        self.src_host = src_host # source
        self.src_port = src_port # source
        self.dst_host = dst_host # destination
        self.dst_port = dst_port # destination
        self.dbs = dbs[:] if dbs else None # default, all of databases
        self.optime = None
        assert self.src_host
        assert self.src_port
        assert self.dst_host
        assert self.dst_port
        self.username = kwargs.get('username')
        self.password = kwargs.get('password')
        try:
            self.src_mc = MongoClient(self.src_host, self.src_port)
            if self.username and self.password:
                self.src_mc.admin.authenticate(self.username, self.password)
                info('auth with %s %s' % (self.username, self.password))
            self.dst_mc = MongoClient(self.dst_host, self.dst_port)
        except Exception, e:
            raise e

    def __del__(self):
        """Destructor."""
        self.src_mc.close()
        self.dst_mc.close()

    def run(self):
        """Start synchronizing data.
        """
        if not self.init_mongosync_config():
            error_exit('failed to init mongosync config')

        if not self.is_optime_valid:
            ts = self.get_src_optime()
            info('current optime: %s' % ts)

            info('dump database...')
            if self.username and self.password:
                res = db_dump(self.src_host, self.src_port, username=self.username, password=self.password)
            else:
                res = db_dump(self.src_host, self.src_port)
            if not res:
                error_exit('dump database failed')

            # TODO
            # drop databases

            info('restore database...')
            if not db_restore(self.dst_host, self.dst_port):
                error_exit('restore database failed')

            info('update optime...')
            self.set_optime(ts)

        info('start syncing...')
        self.oplog_sync()

    def load_config(self, filepath):
        """Load config.
        """
        pass

    def init_mongosync_config(self):
        """Initialize synchronization config on destination mongodb instance.
        """
        # configure 'SyncTo' in local.qiyi.mongosync_config
        source = '%s:%d' % (self.src_host, self.src_port)
        db = self.dst_mc['local']
        coll = db['qiyi_mongosync_config']
        cursor = coll.find({'_id': 'mongosync'})
        if cursor.count() == 0:
            coll.insert({'_id': 'mongosync', 'syncTo': source})
            info('create mongosync config, syncTo %s:%d' % (self.src_host, self.src_port))
        elif cursor.count() == 1:
            current_source = cursor[0].get('syncTo')
            if current_source:
                if current_source != source:
                    error('mongosync config conflicted, already syncTo: %s' % current_source)
                    return False
            else:
                coll.update({'_id': 'mongosync'}, {'$set': {'syncTo': source}})
                info('create mongosync config, syncTo %s:%d' % (self.src_host, self.src_port))
        elif cursor.count() > 1:
            error('inconsistent mongosync config, too many items')
            return False

        # TODO
        # create capped collection for store oplog

        info('init mongosync config done')
        return True

    @property
    def is_optime_valid(self):
        """Check if the optime is out of date.
        """
        optime = self.get_dst_optime()
        if optime:
            cursor = self.src_mc['local']['oplog.rs'].find({'ts': {'$lt': optime}})
            if cursor:
                self.optime = optime
                return True
        return False

    def get_dst_optime(self):
        """Get optime of destination mongod.
        """
        ts = None
        doc = self.dst_mc['local']['qiyi_optime'].find_one({'_id': 'optime'})
        if doc:
            ts = doc.get('optime')
        return ts

    def get_src_optime(self):
        """Get current optime of source mongod.
        """
        ts = None
        db = self.src_mc['admin']
        rs_status = db.command({'replSetGetStatus': 1})
        members = rs_status.get('members')
        if members:
            for member in members:
                role = member.get('stateStr')
                if role == 'PRIMARY':
                    ts = member.get('optime')
                    break
        return ts

    def set_optime(self, optime):
        """Update optime to destination mongod.
        """
        self.optime = optime
        self.dst_mc['local']['qiyi_optime'].update({'_id': 'optime'}, {'$set': {'optime': self.optime}}, upsert=True)

    def oplog_sync(self):
        cursor = self.src_mc['local']db['oplog.rs'].find({'ts': {'$gte': self.optime}}, tailable=True)

        # make sure of that the oplog is invalid
        if cursor.count() == 0 or cursor[0]['ts'] != self.optime:
            error('oplog of destination mongod is out of date')
            return False

        # skip the first oplog-entry
        cursor.skip(1)

        n = 0
        while True:
            if not cursor.alive:
                error('cursor is dead')
                break
            try:
                oplog = cursor.next()
                if oplog:
                    n += 1
                    info(n)
                    info('op: %s' % oplog['op'])
                    # parse
                    ts = oplog['ts']
                    op = oplog['op'] # 'n' or 'i' or 'u' or 'c' or 'd'
                    ns = oplog['ns']
                    try:
                        dbname = ns.split('.', 1)[0]
                        db = self.dst_mc[dbname]
                        if op == 'i': # insert
                            info('ns: %s' % ns)
                            collname = ns.split('.', 1)[1]
                            coll = db[collname]
                            coll.insert(oplog['o'])
                        elif op == 'u': # update
                            info('ns: %s' % ns)
                            collname = ns.split('.', 1)[1]
                            coll = db[collname]
                            coll.update(oplog['o2'], oplog['o'])
                        elif op == 'd': # delete
                            info('ns: %s' % ns)
                            collname = ns.split('.', 1)[1]
                            coll = db[collname]
                            coll.remove(oplog['o'])
                        elif op == 'c': # command
                            info('db: %s' % dbname)
                            db.command(oplog['o'])
                        elif op == 'n': # no-op
                            info('no-op')
                        else:
                            error('unknown command: %s' % oplog)
                        # update local.qiyi.oplog
                        db = self.dst_mc['local']
                        coll = db['qiyi_mongosync_oplog']
                        coll.insert(oplog, check_keys=False)
                        info('apply oplog done: %s' % oplog)
                        self.set_optime(ts)
                    except Exception, e:
                        error(e)
                        error('apply oplog failed: %s' % oplog)
            except Exception, e:
                time.sleep(0.1)

def parse_args():
    """Parse and check arguments.
    """
    parser = argparse.ArgumentParser(description='Synchronization from a replicaSet to another mongo instance.')
    parser.add_argument('--from', nargs='?', required=True, help='the source mongo instance')
    parser.add_argument('--to', nargs='?', required=True, help='the destination mongo instance')
    parser.add_argument('--db', nargs='+', required=False, help='the names of databases to be synchronized')
    parser.add_argument('--oplog', action='store_true', help='enable continuous synchronization')
    parser.add_argument('--username', nargs='?', required=False, help='username')
    parser.add_argument('--password', nargs='?', required=False, help='password')
    #parser.add_argument('--help', nargs='?', required=False, help='help information')
    args = vars(parser.parse_args())
    src_host = args['from'].split(':', 1)[0]
    src_port = int(args['from'].split(':', 1)[1])
    dst_host = args['to'].split(':', 1)[0]
    dst_port = int(args['to'].split(':', 1)[1])
    db = args['db']
    username = args['username']
    password = args['password']
    assert src_host
    assert src_port
    assert dst_host
    assert dst_port
    return src_host, src_port, dst_host, dst_port, db, username, password

def main():
    #src_host, src_port, dst_host, dst_port, db, username, password = parse_args()
    #syncer = MongoSynchronizer(src_host, src_port, dst_host, dst_port, db, username=username, password=password)
    syncer = MongoSynchronizer(
            settings.src_host,
            settings.src_port,
            settings.dst_host,
            settings.dst_port,
            None)
    syncer.run()
    sys.exit(0)

if __name__ == '__main__':
    main()
