# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os

from oslo_concurrency import lockutils
from oslo_log import log as logging
from oslo_service import loopingcall
import six

from os_brick import exception
from os_brick.i18n import _
from os_brick import initiator
from os_brick.initiator.connectors import base
from os_brick.initiator import linuxfc
from os_brick import utils

synchronized = lockutils.synchronized_with_prefix('os-brick-')

LOG = logging.getLogger(__name__)


class FibreChannelConnector(base.BaseLinuxConnector):
    """Connector class to attach/detach Fibre Channel volumes."""

    def __init__(self, root_helper, driver=None,
                 execute=None, use_multipath=False,
                 device_scan_attempts=initiator.DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):
        self._linuxfc = linuxfc.LinuxFibreChannel(root_helper, execute)
        super(FibreChannelConnector, self).__init__(
            root_helper, driver=driver,
            execute=execute,
            device_scan_attempts=device_scan_attempts,
            *args, **kwargs)
        self.use_multipath = use_multipath

    def set_execute(self, execute):
        super(FibreChannelConnector, self).set_execute(execute)
        self._linuxscsi.set_execute(execute)
        self._linuxfc.set_execute(execute)

    @staticmethod
    def get_connector_properties(root_helper, *args, **kwargs):
        """The Fibre Channel connector properties."""
        props = {}
        fc = linuxfc.LinuxFibreChannel(root_helper,
                                       execute=kwargs.get('execute'))

        wwpns = fc.get_fc_wwpns()
        if wwpns:
            props['wwpns'] = wwpns
        wwnns = fc.get_fc_wwnns()
        if wwnns:
            props['wwnns'] = wwnns

        return props

    def get_search_path(self):
        """Where do we look for FC based volumes."""
        return '/dev/disk/by-path'

    def _add_targets_to_connection_properties(self, connection_properties):
        target_wwn = connection_properties.get('target_wwn')
        target_wwns = connection_properties.get('target_wwns')
        if target_wwns:
            wwns = target_wwns
        elif isinstance(target_wwn, list):
            wwns = target_wwn
        elif isinstance(target_wwn, six.string_types):
            wwns = [target_wwn]
        else:
            wwns = []

        target_lun = connection_properties.get('target_lun', 0)
        target_luns = connection_properties.get('target_luns')
        if target_luns:
            luns = target_luns
        elif isinstance(target_lun, int):
            luns = [target_lun]
        else:
            luns = []

        if len(luns) == len(wwns):
            # Handles single wwwn + lun or multiple, potentially
            # different wwns or luns
            targets = list(zip(wwns, luns))
        elif len(luns) == 1 and len(wwns) > 1:
            # For the case of multiple wwns, but a single lun (old path)
            targets = []
            for wwn in wwns:
                targets.append((wwn, luns[0]))
        else:
            # Something is wrong, this shouldn't happen.
            msg = _("Unable to find potential volume paths for FC device "
                    "with luns: %(luns)s and wwns: %(wwns)s.") % {
                        "luns": luns, "wwns": wwns}
            LOG.error(msg)
            raise exception.VolumePathsNotFound(msg)

        connection_properties['targets'] = targets

        wwpn_lun_map = dict()
        for wwpn, lun in targets:
            wwpn_lun_map[wwpn] = lun

        # If there is an initiator_target_map we can update it too
        if 'initiator_target_map' in connection_properties:
            itmap = connection_properties['initiator_target_map']
            new_itmap = dict()
            for init_wwpn in itmap:
                target_wwpns = itmap[init_wwpn]
                init_targets = []
                for target_wwpn in target_wwpns:
                    if target_wwpn in wwpn_lun_map:
                        init_targets.append((target_wwpn,
                                            wwpn_lun_map[target_wwpn]))
                new_itmap[init_wwpn] = init_targets
            connection_properties['initiator_target_lun_map'] = new_itmap

        return connection_properties

    def _get_possible_volume_paths(self, connection_properties, hbas):
        targets = connection_properties['targets']
        possible_devs = self._get_possible_devices(hbas, targets)
        host_paths = self._get_host_devices(possible_devs)
        return host_paths

    def get_volume_paths(self, connection_properties):
        volume_paths = []
        # first fetch all of the potential paths that might exist
        # how the FC fabric is zoned may alter the actual list
        # that shows up on the system.  So, we verify each path.
        hbas = self._linuxfc.get_fc_hbas_info()
        device_paths = self._get_possible_volume_paths(
            connection_properties, hbas)
        for path in device_paths:
            if os.path.exists(path):
                volume_paths.append(path)

        return volume_paths

    @utils.trace
    @synchronized('extend_volume')
    def extend_volume(self, connection_properties):
        """Update the local kernel's size information.

        Try and update the local kernel's size information
        for an FC volume.
        """
        connection_properties = self._add_targets_to_connection_properties(
            connection_properties)

        volume_paths = self.get_volume_paths(connection_properties)
        if volume_paths:
            return self._linuxscsi.extend_volume(volume_paths)
        else:
            LOG.warning("Couldn't find any volume paths on the host to "
                        "extend volume for %(props)s",
                        {'props': connection_properties})
            raise exception.VolumePathsNotFound()

    @utils.trace
    @synchronized('connect_volume')
    def connect_volume(self, connection_properties):
        """Attach the volume to instance_name.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
        :type connection_properties: dict
        :returns: dict

        connection_properties for Fibre Channel must include:
        target_wwn - World Wide Name
        target_lun - LUN id of the volume
        """
        device_info = {'type': 'block'}

        connection_properties = self._add_targets_to_connection_properties(
            connection_properties)

        hbas = self._linuxfc.get_fc_hbas_info()
        if not hbas:
            LOG.warning("We are unable to locate any Fibre Channel devices.")
            raise exception.NoFibreChannelHostsFound()

        host_devices = self._get_possible_volume_paths(
            connection_properties, hbas)

        # The /dev/disk/by-path/... node is not always present immediately
        # We only need to find the first device.  Once we see the first device
        # multipath will have any others.
        def _wait_for_device_discovery(host_devices):
            for device in host_devices:
                LOG.debug("Looking for Fibre Channel dev %(device)s",
                          {'device': device})
                if os.path.exists(device) and self.check_valid_device(device):
                    self.host_device = device
                    # get the /dev/sdX device.  This is used
                    # to find the multipath device.
                    self.device_name = os.path.realpath(device)
                    raise loopingcall.LoopingCallDone()

            if self.tries >= self.device_scan_attempts:
                LOG.error("Fibre Channel volume device not found.")
                raise exception.NoFibreChannelVolumeDeviceFound()

            LOG.info("Fibre Channel volume device not yet found. "
                     "Will rescan & retry.  Try number: %(tries)s.",
                     {'tries': self.tries})

            self._linuxfc.rescan_hosts(hbas, connection_properties)
            self.tries = self.tries + 1

        self.host_device = None
        self.device_name = None
        self.tries = 0
        timer = loopingcall.FixedIntervalLoopingCall(
            _wait_for_device_discovery, host_devices)
        timer.start(interval=2).wait()

        LOG.debug("Found Fibre Channel volume %(name)s "
                  "(after %(tries)s rescans.)",
                  {'name': self.device_name, 'tries': self.tries})

        # find out the WWN of the device
        device_wwn = self._linuxscsi.get_scsi_wwn(self.host_device)
        LOG.debug("Device WWN = '%(wwn)s'", {'wwn': device_wwn})
        device_info['scsi_wwn'] = device_wwn

        # see if the new drive is part of a multipath
        # device.  If so, we'll use the multipath device.
        if self.use_multipath:
            (device_path, multipath_id) = (super(
                FibreChannelConnector, self)._discover_mpath_device(
                device_wwn, connection_properties, self.device_name))
            if multipath_id:
                # only set the multipath_id if we found one
                device_info['multipath_id'] = multipath_id

        else:
            device_path = self.host_device

        device_info['path'] = device_path
        return device_info

    def _get_host_devices(self, possible_devs):
        """Compute the device paths on the system with an id, wwn, and lun

        :param possible_devs: list of (pci_id, wwn, lun) tuples
        :return: list of device paths on the system based on the possible_devs
        """
        host_devices = []
        for pci_num, target_wwn, lun in possible_devs:
            host_device = "/dev/disk/by-path/pci-%s-fc-%s-lun-%s" % (
                pci_num,
                target_wwn,
                self._linuxscsi.process_lun_id(lun))
            host_devices.append(host_device)
        return host_devices

    def _get_possible_devices(self, hbas, targets):
        """Compute the possible fibre channel device options.

        :param hbas: available hba devices.
        :param targets: tuple of possible wwn addresses and lun combinations.

        :returns: list of (pci_id, wwn, lun) tuples

        Given one or more wwn (mac addresses for fibre channel) ports
        do the matrix math to figure out a set of pci device, wwn
        tuples that are potentially valid (they won't all be). This
        provides a search space for the device connection.

        """
        raw_devices = []
        for hba in hbas:
            pci_num = self._get_pci_num(hba)
            if pci_num is not None:
                for wwn, lun in targets:
                    target_wwn = "0x%s" % wwn.lower()
                    raw_devices.append((pci_num, target_wwn, lun))
        return raw_devices

    @utils.trace
    @synchronized('connect_volume')
    def disconnect_volume(self, connection_properties, device_info,
                          force=False, ignore_errors=False):
        """Detach the volume from instance_name.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
        :type connection_properties: dict
        :param device_info: historical difference, but same as connection_props
        :type device_info: dict

        connection_properties for Fibre Channel must include:
        target_wwn - World Wide Name
        target_lun - LUN id of the volume
        """

        devices = []
        wwn = None

        connection_properties = self._add_targets_to_connection_properties(
            connection_properties)

        volume_paths = self.get_volume_paths(connection_properties)
        mpath_path = None
        for path in volume_paths:
            real_path = self._linuxscsi.get_name_from_path(path)
            if self.use_multipath and not mpath_path:
                wwn = self._linuxscsi.get_scsi_wwn(path)
                mpath_path = self._linuxscsi.find_multipath_device_path(wwn)
                if mpath_path:
                    self._linuxscsi.flush_multipath_device(mpath_path)
            device_info = self._linuxscsi.get_device_info(real_path)
            devices.append(device_info)

        LOG.debug("devices to remove = %s", devices)
        self._remove_devices(connection_properties, devices, device_info)

    def _remove_devices(self, connection_properties, devices, device_info):
        # There may have been more than 1 device mounted
        # by the kernel for this volume.  We have to remove
        # all of them
        path_used = self._linuxscsi.get_dev_path(connection_properties,
                                                 device_info)
        was_multipath = '/pci-' not in path_used
        for device in devices:
            device_path = device['device']
            flush = self._linuxscsi.requires_flush(device_path,
                                                   path_used,
                                                   was_multipath)
            self._linuxscsi.remove_scsi_device(device_path, flush=flush)

    def _get_pci_num(self, hba):
        # NOTE(walter-boring)
        # device path is in format of (FC and FCoE) :
        # /sys/devices/pci0000:00/0000:00:03.0/0000:05:00.3/host2/fc_host/host2
        # /sys/devices/pci0000:20/0000:20:03.0/0000:21:00.2/net/ens2f2/ctlr_2
        # /host3/fc_host/host3
        # we always want the value prior to the host or net value
        if hba is not None:
            if "device_path" in hba:
                device_path = hba['device_path'].split('/')
                for index, value in enumerate(device_path):
                    if value.startswith('net') or value.startswith('host'):
                        return device_path[index - 1]
        return None
