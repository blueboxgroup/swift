#! /usr/bin/env python
# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from errno import EEXIST
from itertools import islice, izip
from os import mkdir
from os.path import basename, abspath, dirname, exists, join as pathjoin
from sys import argv as sys_argv, exit, stderr
from textwrap import wrap
from time import time
import optparse
import math

from swift.common import exceptions
from swift.common.ring import RingBuilder, Ring
from swift.common.ring.builder import MAX_BALANCE
from swift.common.utils import lock_parent_directory
from swift.common.ring.utils import parse_search_value, parse_args, \
    build_dev_from_opts, parse_builder_ring_filename_args, find_parts, \
    dispersion_report

MAJOR_VERSION = 1
MINOR_VERSION = 3
EXIT_SUCCESS = 0
EXIT_WARNING = 1
EXIT_ERROR = 2

global argv, backup_dir, builder, builder_file, ring_file
argv = backup_dir = builder = builder_file = ring_file = None


def format_device(dev):
    """
    Format a device for display.
    """
    copy_dev = dev.copy()
    for key in ('ip', 'replication_ip'):
        if ':' in copy_dev[key]:
            copy_dev[key] = '[' + copy_dev[key] + ']'
    return ('d%(id)sr%(region)sz%(zone)s-%(ip)s:%(port)sR'
            '%(replication_ip)s:%(replication_port)s/%(device)s_'
            '"%(meta)s"' % copy_dev)


def _parse_add_values(argvish):
    """
    Parse devices to add as specified on the command line.

    Will exit on error and spew warnings.

    :returns: array of device dicts
    """

    opts, args = parse_args(argvish)

    # We'll either parse the all-in-one-string format or the --options format,
    # but not both. If both are specified, raise an error.
    opts_used = opts.region or opts.zone or opts.ip or opts.port or \
        opts.device or opts.weight or opts.meta

    if len(args) > 0 and opts_used:
        print Commands.add.__doc__.strip()
        exit(EXIT_ERROR)
    elif len(args) > 0:
        if len(args) % 2 != 0:
            print Commands.add.__doc__.strip()
            exit(EXIT_ERROR)

        parsed_devs = []
        devs_and_weights = izip(islice(args, 0, len(args), 2),
                                islice(args, 1, len(args), 2))

        for devstr, weightstr in devs_and_weights:
            region = 1
            rest = devstr
            if devstr.startswith('r'):
                i = 1
                while i < len(devstr) and devstr[i].isdigit():
                    i += 1
                region = int(devstr[1:i])
                rest = devstr[i:]
            else:
                stderr.write("WARNING: No region specified for %s. "
                             "Defaulting to region 1.\n" % devstr)

            if not rest.startswith('z'):
                print 'Invalid add value: %s' % devstr
                exit(EXIT_ERROR)
            i = 1
            while i < len(rest) and rest[i].isdigit():
                i += 1
            zone = int(rest[1:i])
            rest = rest[i:]

            if not rest.startswith('-'):
                print 'Invalid add value: %s' % devstr
                print "The on-disk ring builder is unchanged.\n"
                exit(EXIT_ERROR)
            i = 1
            if rest[i] == '[':
                i += 1
                while i < len(rest) and rest[i] != ']':
                    i += 1
                i += 1
                ip = rest[1:i].lstrip('[').rstrip(']')
                rest = rest[i:]
            else:
                while i < len(rest) and rest[i] in '0123456789.':
                    i += 1
                ip = rest[1:i]
                rest = rest[i:]

            if not rest.startswith(':'):
                print 'Invalid add value: %s' % devstr
                print "The on-disk ring builder is unchanged.\n"
                exit(EXIT_ERROR)
            i = 1
            while i < len(rest) and rest[i].isdigit():
                i += 1
            port = int(rest[1:i])
            rest = rest[i:]

            replication_ip = ip
            replication_port = port
            if rest.startswith('R'):
                i = 1
                if rest[i] == '[':
                    i += 1
                    while i < len(rest) and rest[i] != ']':
                        i += 1
                    i += 1
                    replication_ip = rest[1:i].lstrip('[').rstrip(']')
                    rest = rest[i:]
                else:
                    while i < len(rest) and rest[i] in '0123456789.':
                        i += 1
                    replication_ip = rest[1:i]
                    rest = rest[i:]

                if not rest.startswith(':'):
                    print 'Invalid add value: %s' % devstr
                    print "The on-disk ring builder is unchanged.\n"
                    exit(EXIT_ERROR)
                i = 1
                while i < len(rest) and rest[i].isdigit():
                    i += 1
                replication_port = int(rest[1:i])
                rest = rest[i:]

            if not rest.startswith('/'):
                print 'Invalid add value: %s' % devstr
                print "The on-disk ring builder is unchanged.\n"
                exit(EXIT_ERROR)
            i = 1
            while i < len(rest) and rest[i] != '_':
                i += 1
            device_name = rest[1:i]
            rest = rest[i:]

            meta = ''
            if rest.startswith('_'):
                meta = rest[1:]

            try:
                weight = float(weightstr)
            except ValueError:
                print 'Invalid weight value: %s' % weightstr
                print "The on-disk ring builder is unchanged.\n"
                exit(EXIT_ERROR)

            if weight < 0:
                print 'Invalid weight value (must be positive): %s' % weightstr
                print "The on-disk ring builder is unchanged.\n"
                exit(EXIT_ERROR)

            parsed_devs.append({'region': region, 'zone': zone, 'ip': ip,
                                'port': port, 'device': device_name,
                                'replication_ip': replication_ip,
                                'replication_port': replication_port,
                                'weight': weight, 'meta': meta})
        return parsed_devs
    else:
        try:
            dev = build_dev_from_opts(opts)
        except ValueError as e:
            print e
            print "The on-disk ring builder is unchanged.\n"
            exit(EXIT_ERROR)
        return [dev]


class Commands(object):

    def unknown():
        print 'Unknown command: %s' % argv[2]
        exit(EXIT_ERROR)

    def create():
        """
swift-ring-builder <builder_file> create <part_power> <replicas>
                                         <min_part_hours>
    Creates <builder_file> with 2^<part_power> partitions and <replicas>.
    <min_part_hours> is number of hours to restrict moving a partition more
    than once.
        """
        if len(argv) < 6:
            print Commands.create.__doc__.strip()
            exit(EXIT_ERROR)
        builder = RingBuilder(int(argv[3]), float(argv[4]), int(argv[5]))
        backup_dir = pathjoin(dirname(argv[1]), 'backups')
        try:
            mkdir(backup_dir)
        except OSError as err:
            if err.errno != EEXIST:
                raise
        builder.save(pathjoin(backup_dir, '%d.' % time() + basename(argv[1])))
        builder.save(argv[1])
        exit(EXIT_SUCCESS)

    def default():
        """
swift-ring-builder <builder_file>
    Shows information about the ring and the devices within.
        """
        print '%s, build version %d' % (argv[1], builder.version)
        regions = 0
        zones = 0
        balance = 0
        dev_count = 0
        if builder.devs:
            regions = len(set(d['region'] for d in builder.devs
                              if d is not None))
            zones = len(set((d['region'], d['zone']) for d in builder.devs
                            if d is not None))
            dev_count = len([dev for dev in builder.devs
                             if dev is not None])
            balance = builder.get_balance()
        print '%d partitions, %.6f replicas, %d regions, %d zones, ' \
              '%d devices, %.02f balance, %.02f dispersion' % (
                  builder.parts, builder.replicas, regions, zones, dev_count,
                  balance, builder.dispersion)
        print 'The minimum number of hours before a partition can be ' \
              'reassigned is %s' % builder.min_part_hours
        print 'The overload factor is %.6f' % builder.overload
        if builder.devs:
            print 'Devices:    id  region  zone      ip address  port  ' \
                  'replication ip  replication port      name ' \
                  'weight partitions balance meta'
            weighted_parts = builder.parts * builder.replicas / \
                sum(d['weight'] for d in builder.devs if d is not None)
            for dev in builder.devs:
                if dev is None:
                    continue
                if not dev['weight']:
                    if dev['parts']:
                        balance = MAX_BALANCE
                    else:
                        balance = 0
                else:
                    balance = 100.0 * dev['parts'] / \
                        (dev['weight'] * weighted_parts) - 100.0
                print('         %5d %7d %5d %15s %5d %15s %17d %9s %6.02f '
                      '%10s %7.02f %s' %
                      (dev['id'], dev['region'], dev['zone'], dev['ip'],
                       dev['port'], dev['replication_ip'],
                       dev['replication_port'], dev['device'], dev['weight'],
                       dev['parts'], balance, dev['meta']))
        exit(EXIT_SUCCESS)

    def search():
        """
swift-ring-builder <builder_file> search <search-value>
    Shows information about matching devices.
        """
        if len(argv) < 4:
            print Commands.search.__doc__.strip()
            print
            print parse_search_value.__doc__.strip()
            exit(EXIT_ERROR)
        devs = builder.search_devs(parse_search_value(argv[3]))
        if not devs:
            print 'No matching devices found'
            exit(EXIT_ERROR)
        print 'Devices:    id  region  zone      ip address  port  ' \
              'replication ip  replication port      name weight partitions ' \
              'balance meta'
        weighted_parts = builder.parts * builder.replicas / \
            sum(d['weight'] for d in builder.devs if d is not None)
        for dev in devs:
            if not dev['weight']:
                if dev['parts']:
                    balance = MAX_BALANCE
                else:
                    balance = 0
            else:
                balance = 100.0 * dev['parts'] / \
                    (dev['weight'] * weighted_parts) - 100.0
            print('         %5d %7d %5d %15s %5d %15s %17d %9s %6.02f %10s '
                  '%7.02f %s' %
                  (dev['id'], dev['region'], dev['zone'], dev['ip'],
                   dev['port'], dev['replication_ip'], dev['replication_port'],
                   dev['device'], dev['weight'], dev['parts'], balance,
                   dev['meta']))
        exit(EXIT_SUCCESS)

    def list_parts():
        """
swift-ring-builder <builder_file> list_parts <search-value> [<search-value>] ..
    Returns a 2 column list of all the partitions that are assigned to any of
    the devices matching the search values given. The first column is the
    assigned partition number and the second column is the number of device
    matches for that partition. The list is ordered from most number of matches
    to least. If there are a lot of devices to match against, this command
    could take a while to run.
        """
        if len(argv) < 4:
            print Commands.list_parts.__doc__.strip()
            print
            print parse_search_value.__doc__.strip()
            exit(EXIT_ERROR)

        if not builder._replica2part2dev:
            print('Specified builder file \"%s\" is not rebalanced yet. '
                  'Please rebalance first.' % argv[1])
            exit(EXIT_ERROR)

        sorted_partition_count = find_parts(builder, argv)

        if not sorted_partition_count:
            print 'No matching devices found'
            exit(EXIT_ERROR)

        print 'Partition   Matches'
        for partition, count in sorted_partition_count:
            print '%9d   %7d' % (partition, count)
        exit(EXIT_SUCCESS)

    def add():
        """
swift-ring-builder <builder_file> add
    [r<region>]z<zone>-<ip>:<port>[R<r_ip>:<r_port>]/<device_name>_<meta>
     <weight>
    [[r<region>]z<zone>-<ip>:<port>[R<r_ip>:<r_port>]/<device_name>_<meta>
     <weight>] ...

    Where <r_ip> and <r_port> are replication ip and port.

or

swift-ring-builder <builder_file> add
    --region <region> --zone <zone> --ip <ip> --port <port>
    [--replication-ip <r_ip> --replication-port <r_port>]
    --device <device_name> --weight <weight>
    [--meta <meta>]

    Adds devices to the ring with the given information. No partitions will be
    assigned to the new device until after running 'rebalance'. This is so you
    can make multiple device changes and rebalance them all just once.
        """
        if len(argv) < 5 or len(argv) % 2 != 1:
            print Commands.add.__doc__.strip()
            exit(EXIT_ERROR)

        for new_dev in _parse_add_values(argv[3:]):
            for dev in builder.devs:
                if dev is None:
                    continue
                if dev['ip'] == new_dev['ip'] and \
                        dev['port'] == new_dev['port'] and \
                        dev['device'] == new_dev['device']:
                    print 'Device %d already uses %s:%d/%s.' % \
                          (dev['id'], dev['ip'], dev['port'], dev['device'])
                    print "The on-disk ring builder is unchanged.\n"
                    exit(EXIT_ERROR)
            builder.add_dev(new_dev)
            print('Device %s weight %s' %
                  (format_device(new_dev), new_dev['weight']))

        builder.save(argv[1])
        exit(EXIT_SUCCESS)

    def set_weight():
        """
swift-ring-builder <builder_file> set_weight <search-value> <weight>
    [<search-value> <weight] ...

    Resets the devices' weights. No partitions will be reassigned to or from
    the device until after running 'rebalance'. This is so you can make
    multiple device changes and rebalance them all just once.
        """
        if len(argv) < 5 or len(argv) % 2 != 1:
            print Commands.set_weight.__doc__.strip()
            print
            print parse_search_value.__doc__.strip()
            exit(EXIT_ERROR)

        devs_and_weights = izip(islice(argv, 3, len(argv), 2),
                                islice(argv, 4, len(argv), 2))
        for devstr, weightstr in devs_and_weights:
            devs = builder.search_devs(parse_search_value(devstr))
            weight = float(weightstr)
            if not devs:
                print("Search value \"%s\" matched 0 devices.\n"
                      "The on-disk ring builder is unchanged.\n"
                      % devstr)
                exit(EXIT_ERROR)
            if len(devs) > 1:
                print 'Matched more than one device:'
                for dev in devs:
                    print '    %s' % format_device(dev)
                if raw_input('Are you sure you want to update the weight for '
                             'these %s devices? (y/N) ' % len(devs)) != 'y':
                    print 'Aborting device modifications'
                    exit(EXIT_ERROR)
            for dev in devs:
                builder.set_dev_weight(dev['id'], weight)
                print '%s weight set to %s' % (format_device(dev),
                                               dev['weight'])
        builder.save(argv[1])
        exit(EXIT_SUCCESS)

    def set_info():
        """
swift-ring-builder <builder_file> set_info
    <search-value> <ip>:<port>[R<r_ip>:<r_port>]/<device_name>_<meta>
    [<search-value> <ip>:<port>[R<r_ip>:<r_port>]/<device_name>_<meta>] ...

    Where <r_ip> and <r_port> are replication ip and port.

    For each search-value, resets the matched device's information.
    This information isn't used to assign partitions, so you can use
    'write_ring' afterward to rewrite the current ring with the newer
    device information. Any of the parts are optional in the final
    <ip>:<port>/<device_name>_<meta> parameter; just give what you
    want to change. For instance set_info d74 _"snet: 5.6.7.8" would
    just update the meta data for device id 74.
        """
        if len(argv) < 5 or len(argv) % 2 != 1:
            print Commands.set_info.__doc__.strip()
            print
            print parse_search_value.__doc__.strip()
            exit(EXIT_ERROR)

        searches_and_changes = izip(islice(argv, 3, len(argv), 2),
                                    islice(argv, 4, len(argv), 2))

        for search_value, change_value in searches_and_changes:
            devs = builder.search_devs(parse_search_value(search_value))
            change = []
            if len(change_value) and change_value[0].isdigit():
                i = 1
                while (i < len(change_value) and
                       change_value[i] in '0123456789.'):
                    i += 1
                change.append(('ip', change_value[:i]))
                change_value = change_value[i:]
            elif len(change_value) and change_value[0] == '[':
                i = 1
                while i < len(change_value) and change_value[i] != ']':
                    i += 1
                i += 1
                change.append(('ip', change_value[:i].lstrip('[').rstrip(']')))
                change_value = change_value[i:]
            if change_value.startswith(':'):
                i = 1
                while i < len(change_value) and change_value[i].isdigit():
                    i += 1
                change.append(('port', int(change_value[1:i])))
                change_value = change_value[i:]
            if change_value.startswith('R'):
                change_value = change_value[1:]
                if len(change_value) and change_value[0].isdigit():
                    i = 1
                    while (i < len(change_value) and
                           change_value[i] in '0123456789.'):
                        i += 1
                    change.append(('replication_ip', change_value[:i]))
                    change_value = change_value[i:]
                elif len(change_value) and change_value[0] == '[':
                    i = 1
                    while i < len(change_value) and change_value[i] != ']':
                        i += 1
                    i += 1
                    change.append(('replication_ip',
                                   change_value[:i].lstrip('[').rstrip(']')))
                    change_value = change_value[i:]
                if change_value.startswith(':'):
                    i = 1
                    while i < len(change_value) and change_value[i].isdigit():
                        i += 1
                    change.append(('replication_port', int(change_value[1:i])))
                    change_value = change_value[i:]
            if change_value.startswith('/'):
                i = 1
                while i < len(change_value) and change_value[i] != '_':
                    i += 1
                change.append(('device', change_value[1:i]))
                change_value = change_value[i:]
            if change_value.startswith('_'):
                change.append(('meta', change_value[1:]))
                change_value = ''
            if change_value or not change:
                raise ValueError('Invalid set info change value: %s' %
                                 repr(argv[4]))
            if not devs:
                print("Search value \"%s\" matched 0 devices.\n"
                      "The on-disk ring builder is unchanged.\n"
                      % search_value)
                exit(EXIT_ERROR)
            if len(devs) > 1:
                print 'Matched more than one device:'
                for dev in devs:
                    print '    %s' % format_device(dev)
                if raw_input('Are you sure you want to update the info for '
                             'these %s devices? (y/N) ' % len(devs)) != 'y':
                    print 'Aborting device modifications'
                    exit(EXIT_ERROR)
            for dev in devs:
                orig_dev_string = format_device(dev)
                test_dev = dict(dev)
                for key, value in change:
                    test_dev[key] = value
                for check_dev in builder.devs:
                    if not check_dev or check_dev['id'] == test_dev['id']:
                        continue
                    if check_dev['ip'] == test_dev['ip'] and \
                            check_dev['port'] == test_dev['port'] and \
                            check_dev['device'] == test_dev['device']:
                        print 'Device %d already uses %s:%d/%s.' % \
                              (check_dev['id'], check_dev['ip'],
                               check_dev['port'], check_dev['device'])
                        exit(EXIT_ERROR)
                for key, value in change:
                    dev[key] = value
                print 'Device %s is now %s' % (orig_dev_string,
                                               format_device(dev))
        builder.save(argv[1])
        exit(EXIT_SUCCESS)

    def remove():
        """
swift-ring-builder <builder_file> remove <search-value> [search-value ...]
    Removes the device(s) from the ring. This should normally just be used for
    a device that has failed. For a device you wish to decommission, it's best
    to set its weight to 0, wait for it to drain all its data, then use this
    remove command. This will not take effect until after running 'rebalance'.
    This is so you can make multiple device changes and rebalance them all just
    once.
        """
        if len(argv) < 4:
            print Commands.remove.__doc__.strip()
            print
            print parse_search_value.__doc__.strip()
            exit(EXIT_ERROR)

        for search_value in argv[3:]:
            devs = builder.search_devs(parse_search_value(search_value))
            if not devs:
                print("Search value \"%s\" matched 0 devices.\n"
                      "The on-disk ring builder is unchanged." % search_value)
                exit(EXIT_ERROR)
            if len(devs) > 1:
                print 'Matched more than one device:'
                for dev in devs:
                    print '    %s' % format_device(dev)
                if raw_input('Are you sure you want to remove these %s '
                             'devices? (y/N) ' % len(devs)) != 'y':
                    print 'Aborting device removals'
                    exit(EXIT_ERROR)
            for dev in devs:
                try:
                    builder.remove_dev(dev['id'])
                except exceptions.RingBuilderError as e:
                    print '-' * 79
                    print(
                        "An error occurred while removing device with id %d\n"
                        "This usually means that you attempted to remove\n"
                        "the last device in a ring. If this is the case,\n"
                        "consider creating a new ring instead.\n"
                        "The on-disk ring builder is unchanged.\n"
                        "Original exception message: %s" %
                        (dev['id'], e)
                    )
                    print '-' * 79
                    exit(EXIT_ERROR)

                print '%s marked for removal and will ' \
                      'be removed next rebalance.' % format_device(dev)
        builder.save(argv[1])
        exit(EXIT_SUCCESS)

    def rebalance():
        """
swift-ring-builder <builder_file> rebalance [options]
    Attempts to rebalance the ring by reassigning partitions that haven't been
    recently reassigned.
        """
        usage = Commands.rebalance.__doc__.strip()
        parser = optparse.OptionParser(usage)
        parser.add_option('-f', '--force', action='store_true',
                          help='Force a rebalanced ring to save even '
                          'if < 1% of parts changed')
        parser.add_option('-s', '--seed', help="seed to use for rebalance")
        options, args = parser.parse_args(argv)

        def get_seed(index):
            if options.seed:
                return options.seed
            try:
                return args[index]
            except IndexError:
                pass

        devs_changed = builder.devs_changed
        try:
            last_balance = builder.get_balance()
            parts, balance = builder.rebalance(seed=get_seed(3))
        except exceptions.RingBuilderError as e:
            print '-' * 79
            print("An error has occurred during ring validation. Common\n"
                  "causes of failure are rings that are empty or do not\n"
                  "have enough devices to accommodate the replica count.\n"
                  "Original exception message:\n %s" % e.message
                  )
            print '-' * 79
            exit(EXIT_ERROR)
        if not parts:
            print 'No partitions could be reassigned.'
            print 'Either none need to be or none can be due to ' \
                  'min_part_hours [%s].' % builder.min_part_hours
            exit(EXIT_WARNING)
        # If we set device's weight to zero, currently balance will be set
        # special value(MAX_BALANCE) until zero weighted device return all
        # its partitions. So we cannot check balance has changed.
        # Thus we need to check balance or last_balance is special value.
        if not options.force and \
                not devs_changed and abs(last_balance - balance) < 1 and \
                not (last_balance == MAX_BALANCE and balance == MAX_BALANCE):
            print 'Cowardly refusing to save rebalance as it did not change ' \
                  'at least 1%.'
            exit(EXIT_WARNING)
        try:
            builder.validate()
        except exceptions.RingValidationError as e:
            print '-' * 79
            print("An error has occurred during ring validation. Common\n"
                  "causes of failure are rings that are empty or do not\n"
                  "have enough devices to accommodate the replica count.\n"
                  "Original exception message:\n %s" % e.message
                  )
            print '-' * 79
            exit(EXIT_ERROR)
        print ('Reassigned %d (%.02f%%) partitions. '
               'Balance is now %.02f.  '
               'Dispersion is now %.02f' % (
                   parts, 100.0 * parts / builder.parts,
                   balance,
                   builder.dispersion))
        status = EXIT_SUCCESS
        if builder.dispersion > 0:
            print '-' * 79
            print('NOTE: Dispersion of %.06f indicates some parts are not\n'
                  '      optimally dispersed.\n\n'
                  '      You may want adjust some device weights, increase\n'
                  '      the overload or review the dispersion report.' %
                  builder.dispersion)
            status = EXIT_WARNING
            print '-' * 79
        elif balance > 5 and balance / 100.0 > builder.overload:
            print '-' * 79
            print 'NOTE: Balance of %.02f indicates you should push this ' % \
                  balance
            print '      ring, wait at least %d hours, and rebalance/repush.' \
                  % builder.min_part_hours
            print '-' * 79
            status = EXIT_WARNING
        ts = time()
        builder.get_ring().save(
            pathjoin(backup_dir, '%d.' % ts + basename(ring_file)))
        builder.save(pathjoin(backup_dir, '%d.' % ts + basename(argv[1])))
        builder.get_ring().save(ring_file)
        builder.save(argv[1])
        exit(status)

    def dispersion():
        """
swift-ring-builder <builder_file> dispersion <search_filter> [options]

    Output report on dispersion.

    --verbose option will display dispersion graph broken down by tier

    You can filter which tiers are evaluated to drill down using a regex
    in the optional search_filter arguemnt.

    The reports columns are:

    Tier  : the name of the tier
    parts : the total number of partitions with assignment in the tier
    %     : the percentage of parts in the tier with replicas over assigned
    max   : maximum replicas a part should have assigned at the tier
    0 - N : the number of parts with that many replicas assigned

    e.g.
        Tier:  parts      %   max   0    1    2   3
        r1z1    1022  79.45     1   2  210  784  28

        r1z1 has 1022 total parts assigned, 79% of them have more than the
        recommend max replica count of 1 assigned.  Only 2 parts in the ring
        are *not* assigned in this tier (0 replica count), 210 parts have
        the recommend replica count of 1, 784 have 2 replicas, and 28 sadly
        have all three replicas in this tier.
        """
        status = EXIT_SUCCESS
        if not builder._replica2part2dev:
            print('Specified builder file \"%s\" is not rebalanced yet. '
                  'Please rebalance first.' % argv[1])
            exit(EXIT_ERROR)
        usage = Commands.dispersion.__doc__.strip()
        parser = optparse.OptionParser(usage)
        parser.add_option('-v', '--verbose', action='store_true',
                          help='Display dispersion report for tiers')
        options, args = parser.parse_args(argv)
        if args[3:]:
            search_filter = args[3]
        else:
            search_filter = None
        report = dispersion_report(builder, search_filter=search_filter,
                                   verbose=options.verbose)
        print 'Dispersion is %.06f' % builder.dispersion
        if report['worst_tier']:
            status = EXIT_WARNING
            print 'Worst tier is %.06f (%s)' % (report['max_dispersion'],
                                                report['worst_tier'])
        if report['graph']:
            replica_range = range(int(math.ceil(builder.replicas + 1)))
            part_count_width = '%%%ds' % max(len(str(builder.parts)), 5)
            replica_counts_tmpl = ' '.join(part_count_width for i in
                                           replica_range)
            tiers = (tier for tier, _junk in report['graph'])
            tier_width = max(max(map(len, tiers)), 30)
            header_line = ('%-' + str(tier_width) +
                           's ' + part_count_width +
                           ' %6s %6s ' + replica_counts_tmpl) % tuple(
                               ['Tier', 'Parts', '%', 'Max'] + replica_range)
            underline = '-' * len(header_line)
            print(underline)
            print(header_line)
            print(underline)
            for tier_name, dispersion in report['graph']:
                replica_counts_repr = replica_counts_tmpl % tuple(
                    dispersion['replicas'])
                print ('%-' + str(tier_width) + 's ' + part_count_width +
                       ' %6.02f %6d %s') % (tier_name,
                                            dispersion['placed_parts'],
                                            dispersion['dispersion'],
                                            dispersion['max_replicas'],
                                            replica_counts_repr,
                                            )
        exit(status)

    def validate():
        """
swift-ring-builder <builder_file> validate
    Just runs the validation routines on the ring.
        """
        builder.validate()
        exit(EXIT_SUCCESS)

    def write_ring():
        """
swift-ring-builder <builder_file> write_ring
    Just rewrites the distributable ring file. This is done automatically after
    a successful rebalance, so really this is only useful after one or more
    'set_info' calls when no rebalance is needed but you want to send out the
    new device information.
        """
        ring_data = builder.get_ring()
        if not ring_data._replica2part2dev_id:
            if ring_data.devs:
                print 'Warning: Writing a ring with no partition ' \
                      'assignments but with devices; did you forget to run ' \
                      '"rebalance"?'
            else:
                print 'Warning: Writing an empty ring'
        ring_data.save(
            pathjoin(backup_dir, '%d.' % time() + basename(ring_file)))
        ring_data.save(ring_file)
        exit(EXIT_SUCCESS)

    def write_builder():
        """
swift-ring-builder <ring_file> write_builder [min_part_hours]
    Recreate a builder from a ring file (lossy) if you lost your builder
    backups.  (Protip: don't lose your builder backups).
    [min_part_hours] is one of those numbers lost to the builder,
    you can change it with set_min_part_hours.
        """
        if exists(builder_file):
            print 'Cowardly refusing to overwrite existing ' \
                'Ring Builder file: %s' % builder_file
            exit(EXIT_ERROR)
        if len(argv) > 3:
            min_part_hours = int(argv[3])
        else:
            stderr.write("WARNING: default min_part_hours may not match "
                         "the value in the lost builder.\n")
            min_part_hours = 24
        ring = Ring(ring_file)
        for dev in ring.devs:
            dev.update({
                'parts': 0,
                'parts_wanted': 0,
            })
        builder_dict = {
            'part_power': 32 - ring._part_shift,
            'replicas': float(ring.replica_count),
            'min_part_hours': min_part_hours,
            'parts': ring.partition_count,
            'devs': ring.devs,
            'devs_changed': False,
            'version': 0,
            '_replica2part2dev': ring._replica2part2dev_id,
            '_last_part_moves_epoch': None,
            '_last_part_moves': None,
            '_last_part_gather_start': 0,
            '_remove_devs': [],
        }
        builder = RingBuilder(1, 1, 1)
        builder.copy_from(builder_dict)
        for parts in builder._replica2part2dev:
            for dev_id in parts:
                builder.devs[dev_id]['parts'] += 1
        builder._set_parts_wanted()
        builder.save(builder_file)

    def pretend_min_part_hours_passed():
        builder.pretend_min_part_hours_passed()
        builder.save(argv[1])
        exit(EXIT_SUCCESS)

    def set_min_part_hours():
        """
swift-ring-builder <builder_file> set_min_part_hours <hours>
    Changes the <min_part_hours> to the given <hours>. This should be set to
    however long a full replication/update cycle takes. We're working on a way
    to determine this more easily than scanning logs.
        """
        if len(argv) < 4:
            print Commands.set_min_part_hours.__doc__.strip()
            exit(EXIT_ERROR)
        builder.change_min_part_hours(int(argv[3]))
        print 'The minimum number of hours before a partition can be ' \
              'reassigned is now set to %s' % argv[3]
        builder.save(argv[1])
        exit(EXIT_SUCCESS)

    def set_replicas():
        """
swift-ring-builder <builder_file> set_replicas <replicas>
    Changes the replica count to the given <replicas>. <replicas> may
    be a floating-point value, in which case some partitions will have
    floor(<replicas>) replicas and some will have ceiling(<replicas>)
    in the correct proportions.

    A rebalance is needed to make the change take effect.
    """
        if len(argv) < 4:
            print Commands.set_replicas.__doc__.strip()
            exit(EXIT_ERROR)

        new_replicas = argv[3]
        try:
            new_replicas = float(new_replicas)
        except ValueError:
            print Commands.set_replicas.__doc__.strip()
            print "\"%s\" is not a valid number." % new_replicas
            exit(EXIT_ERROR)

        if new_replicas < 1:
            print "Replica count must be at least 1."
            exit(EXIT_ERROR)

        builder.set_replicas(new_replicas)
        print 'The replica count is now %.6f.' % builder.replicas
        print 'The change will take effect after the next rebalance.'
        builder.save(argv[1])
        exit(EXIT_SUCCESS)

    def set_overload():
        """
swift-ring-builder <builder_file> set_overload <overload>
    Changes the overload factor to the given <overload>.

    A rebalance is needed to make the change take effect.
    """
        if len(argv) < 4:
            print Commands.set_overload.__doc__.strip()
            exit(EXIT_ERROR)

        new_overload = argv[3]
        try:
            new_overload = float(new_overload)
        except ValueError:
            print Commands.set_overload.__doc__.strip()
            print "\"%s\" is not a valid number." % new_overload
            exit(EXIT_ERROR)

        if new_overload < 0:
            print "Overload must be non-negative."
            exit(EXIT_ERROR)

        builder.set_overload(new_overload)
        print 'The overload is now %.6f.' % builder.overload
        print 'The change will take effect after the next rebalance.'
        builder.save(argv[1])
        exit(EXIT_SUCCESS)


def main(arguments=None):
    global argv, backup_dir, builder, builder_file, ring_file
    if arguments:
        argv = arguments
    else:
        argv = sys_argv

    if len(argv) < 2:
        print "swift-ring-builder %(MAJOR_VERSION)s.%(MINOR_VERSION)s\n" % \
              globals()
        print Commands.default.__doc__.strip()
        print
        cmds = [c for c, f in Commands.__dict__.iteritems()
                if f.__doc__ and c[0] != '_' and c != 'default']
        cmds.sort()
        for cmd in cmds:
            print Commands.__dict__[cmd].__doc__.strip()
            print
        print parse_search_value.__doc__.strip()
        print
        for line in wrap(' '.join(cmds), 79, initial_indent='Quick list: ',
                         subsequent_indent='            '):
            print line
        print('Exit codes: 0 = operation successful\n'
              '            1 = operation completed with warnings\n'
              '            2 = error')
        exit(EXIT_SUCCESS)

    builder_file, ring_file = parse_builder_ring_filename_args(argv)

    try:
        builder = RingBuilder.load(builder_file)
    except exceptions.UnPicklingError as e:
        print e
        exit(EXIT_ERROR)
    except (exceptions.FileNotFoundError, exceptions.PermissionError) as e:
        if len(argv) < 3 or argv[2] not in('create', 'write_builder'):
            print e
            exit(EXIT_ERROR)
    except Exception as e:
        print 'Problem occurred while reading builder file: %s. %s' % (
            argv[1], e.message)
        exit(EXIT_ERROR)

    backup_dir = pathjoin(dirname(argv[1]), 'backups')
    try:
        mkdir(backup_dir)
    except OSError as err:
        if err.errno != EEXIST:
            raise

    if len(argv) == 2:
        command = "default"
    else:
        command = argv[2]
    if argv[0].endswith('-safe'):
        try:
            with lock_parent_directory(abspath(argv[1]), 15):
                Commands.__dict__.get(command, Commands.unknown.im_func)()
        except exceptions.LockTimeout:
            print "Ring/builder dir currently locked."
            exit(2)
    else:
        Commands.__dict__.get(command, Commands.unknown.im_func)()


if __name__ == '__main__':
    main()
