# Copyright 2016-2017 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3, as published
# by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.

import contextlib
import crypt
import dbus
import os
import psutil
import pylxd
import shlex
import subprocess
import time

from libertine import Libertine, utils, HostInfo
from .service.manager import LIBERTINE_MANAGER_NAME, LIBERTINE_STORE_PATH


def _get_devices_map():
    devices = {
        '/dev/tty0':   {'path': '/dev/tty0', 'type': 'unix-char'},
        '/dev/tty7':   {'path': '/dev/tty7', 'type': 'unix-char'},
        '/dev/tty8':   {'path': '/dev/tty8', 'type': 'unix-char'},
        '/dev/fb0':    {'path': '/dev/fb0', 'type': 'unix-char'},
        'x11-socket':  {'source': '/tmp/.X11-unix', 'path': '/tmp/.X11-unix', 'type': 'disk', 'optional': 'true'},
    }

    if os.path.exists('/dev/video0'):
        devices['/dev/video0'] = {'path': '/dev/video0', 'type': 'unix-char'}

    # every regular file in /dev/snd
    for f in ['/dev/snd/{}'.format(f) for f in os.listdir('/dev/snd') if not os.path.isdir('/dev/snd/{}'.format(f))]:
        devices[f] = {'path': f, 'type': 'unix-char'}

    # every regular file in /dev/dri
    for f in ['/dev/dri/{}'.format(f) for f in os.listdir('/dev/dri') if not os.path.isdir('/dev/dri/{}'.format(f))]:
        devices[f] = {'path': f, 'type': 'unix-char'}

    # Some devices require special mappings for snap
    if utils.is_snap_environment():
        devices['x11-socket']['source'] = '/var/lib/snapd/hostfs/tmp/.X11-unix'

    return devices

def _readlink(source):
    while os.path.islink(source):
        new_source = os.readlink(source)
        if not os.path.isabs(new_source):
            new_source = os.path.join(os.path.dirname(source), new_source)
        source = new_source

    return source

def _setup_lxd():
    if utils.is_snap_environment():
        utils.get_logger().warning("Running in snap environment, skipping automatic lxd setup.")
        return True

    utils.get_logger().info("Running LXD setup.")
    import pexpect
    child = pexpect.spawnu('sudo libertine-lxd-setup {}'.format(os.environ['USER']), env={'TERM': 'dumb'})

    while True:
        index = child.expect(['.+\[default=.+\].*', pexpect.EOF, pexpect.TIMEOUT,
                              # The following are required for lxd=2.0.x
                              '.+\[yes/no\].*',
                              '.+\(e.g. (?P<example>[a-z0-9\.:]+)\).+'])
        if index == 0:
            child.sendline()
        elif index == 1:
            return True
        elif index == 2:
            return False
        elif index == 3:
            child.sendline('yes')
        elif index == 4:
            child.sendline(child.match.group('example'))

        if child.exitstatus is not None:
            return child.exitstatus == 0


def _setup_bind_mount_service(container, uid, username):
    utils.get_logger().info("Creating mount update shell script")
    script = '''
#!/bin/sh

mkdir -p /run/user/{uid}
chown {username}:{username} /run/user/{uid}
chmod 755 /run/user/{uid}
mount -o bind /var/tmp/run/user/{uid} /run/user/{uid}

chgrp audio /dev/snd/*
chgrp video /dev/dri/*
[ -n /dev/video0 ] && chgrp video /dev/video0
'''[1:-1]
    container.files.put('/usr/bin/libertine-lxd-mount-update', script.format(uid=uid, username=username).encode('utf-8'))
    subprocess.Popen(shlex.split("lxc exec {} -- chmod 755 /usr/bin/libertine-lxd-mount-update".format(container.name)))


def lxd_container(client, container_id):
    try:
        return client.containers.get(container_id)
    except pylxd.exceptions.LXDAPIException:
        return None


def _wait_for_network(container):
    for retries in range(0, 10):
        ping = subprocess.Popen(shlex.split("lxc exec {} -- ping -c 1 ubuntu.com".format(container.name)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = ping.communicate()
        if out:
            utils.get_logger().info("Network connection active")
            return True
        time.sleep(1)
    return False


def lxd_start(container):
    if container.status == 'Stopped':
        container.start(wait=True)
    elif container.status == 'Frozen':
        container.unfreeze(wait=True)

    container.sync(rollback=True) # required for pylxd=2.0.x

    if container.status != 'Running':
        utils.get_logger().error("Container {} failed to start".format(container.name))
        return False

    return True


def lxd_stop(container, wait=True, freeze_on_stop=False):
    if container.status == 'Stopped':
        return True

    if freeze_on_stop:
        container.freeze(wait=wait)
    else:
        container.stop(wait=wait)

    container.sync(rollback=True) # required for pylxd=2.0.x

    if wait:
        if freeze_on_stop and container.status != 'Frozen':
            utils.get_logger().error("Container {} failed to freeze".format(container.name))
            return False
        elif not freeze_on_stop and container.status != 'Stopped':
            utils.get_logger().error("Container {} failed to stop".format(container.name))
            return False

    return True


def _lxd_save(entity, error, wait=True):
    try:
        entity.save(wait=wait)
    except pylxd.exceptions.LXDAPIException as e:
        utils.get_logger().warning('{} {}'.format(error, str(e)))


_CONTAINER_DATA_DIRS = ["/usr/share/applications", "/usr/share/icons", "/usr/local/share/applications", "/usr/share/pixmaps"]


class RestoreDevice(contextlib.ExitStack):
    def __init__(self, container, device):
        super().__init__()
        self._container = container
        self._device = device
        self._source = None
        self.remove()
        self.callback(self.restore)

    def remove(self):
        if self._device in self._container.devices:
            self._source = self._container.devices[self._device]
            del self._container.devices[self._device]
            _lxd_save(self._container, 'Saving bind mounts for container \'{}\' raised:'.format(self._container.name))

    def restore(self):
        if self._source is not None:
            self._container.devices[self._device] = self._source
            _lxd_save(self._container, 'Saving bind mounts for container \'{}\' raised:'.format(self._container.name), False)


def _sync_application_dirs_to_host(container):
    host_root = utils.get_libertine_container_rootfs_path(container.name)
    for container_path in _CONTAINER_DATA_DIRS:
        utils.get_logger().info("Syncing applications directory: {}".format(container_path))
        os.makedirs(os.path.join(host_root, container_path.lstrip("/")), exist_ok=True)
        with RestoreDevice(container, container_path):
            # find a list of files within the container
            find = subprocess.Popen(shlex.split("lxc exec {} -- find {} -type f".format(container.name, container_path)), stdout=subprocess.PIPE)
            stdout, stderr = find.communicate()

        if find.returncode == 0:
            for filepath in stdout.decode('utf-8').strip().split('\n'):
                if filepath:
                    host_path = os.path.join(host_root, filepath.lstrip("/"))
                    if not os.path.exists(host_path):
                        utils.get_logger().debug("Syncing file: {}:{}".format(filepath, host_path))
                        os.makedirs(os.path.dirname(host_path), exist_ok=True)
                        with open(host_path, 'wb') as f:
                            f.write(container.files.get(filepath))


def update_bind_mounts(container, config, home_path):
    userdata_dir = utils.get_libertine_container_home_dir(container.name)

    container.devices.clear()
    container.devices['root'] = {'type': 'disk', 'path': '/'}
    container.devices['home'] = {'type': 'disk', 'path': home_path, 'source': userdata_dir}

    # applications and icons directories
    rootfs_path = utils.get_libertine_container_rootfs_path(container.name)
    for data_dir in _CONTAINER_DATA_DIRS:
        host_path = "{}{}".format(rootfs_path, data_dir)
        os.makedirs(host_path, exist_ok=True)
        container.devices[data_dir] = {'type': 'disk', 'path': data_dir, 'source': host_path}

    if os.path.exists(os.path.join(home_path, '.config', 'dconf')):
        container.devices['dconf'] = {
            'type': 'disk',
            'source': os.path.join(home_path, '.config', 'dconf'),
            'path': os.path.join(home_path, '.config', 'dconf')
        }

    run_user = '/run/user/{}'.format(os.getuid())
    container.devices[run_user] = {'source': run_user, 'path': '/var/tmp{}'.format(run_user), 'type': 'disk'}

    mounts = config.get_container_bind_mounts(container.name)
    if utils.is_snap_environment():
        mounts += [os.path.join(home_path, d) for d in ["Documents", "Downloads", "Music", "Videos", "Pictures"]]
    else:
        mounts += utils.get_common_xdg_user_directories()

    for user_dir in utils.generate_binding_directories(mounts, home_path.rstrip('/')):
        if not os.path.exists(user_dir[0]):
            utils.get_logger().warning('Bind-mount path \'{}\' does not exist.'.format(user_dir[0]))
            continue

        if os.path.isabs(user_dir[1]):
            path = user_dir[1]
        else:
            path = os.path.join(home_path, user_dir[1])
            hostpath = os.path.join(userdata_dir, user_dir[1])
            os.makedirs(hostpath, exist_ok=True)

        utils.get_logger().debug("Mounting {}:{} in container {}".format(user_dir[0], path, container.name))

        container.devices[user_dir[1] or user_dir[0]] = {
                'source': _readlink(user_dir[0]),
                'path': path,
                'optional': 'true',
                'type': 'disk'
        }

    _lxd_save(container, 'Saving bind mounts for container \'{}\' raised:'.format(container.name))


def update_libertine_profile(client):
    try:
        profile = client.profiles.get('libertine')

        utils.get_logger().info('Updating existing lxd profile.')
        profile.devices = _get_devices_map()
        profile.config['raw.idmap'] = 'both 1000 1000'

        _lxd_save(profile, 'Saving libertine lxd profile raised:')
    except pylxd.exceptions.LXDAPIException:
        utils.get_logger().info('Creating libertine lxd profile.')
        client.profiles.create('libertine', config={'raw.idmap': 'both 1000 1000'}, devices=_get_devices_map())


def env_home_path():
    if utils.is_snap_environment():
        return '/home/{}'.format(os.environ['USER'])
    return os.environ['HOME']


class LibertineLXD(Libertine.BaseContainer):
    def __init__(self, name, config):
        super().__init__(name, 'lxd', config)
        self._host_info = HostInfo.HostInfo()
        self._container = None
        self._matchbox_pid = None
        self._manager = None
        self._freeze_on_stop = config.get_freeze_on_stop(self.container_id)

        if not _setup_lxd():
            raise Exception("Failed to setup lxd.")

        self._client = pylxd.Client()
        self._window_manager = None

        if not utils.is_snap_environment():
            try:
                if utils.set_session_dbus_env_var():
                    bus = dbus.SessionBus()
                    self._manager = bus.get_object(LIBERTINE_MANAGER_NAME, LIBERTINE_STORE_PATH)
            except PermissionError as e:
                utils.get_logger().warning("Failed to set dbus session env var")
            except dbus.exceptions.DBusException:
                utils.get_logger().warning("D-Bus Service not found.")

    def create_libertine_container(self, password=None, multiarch=False):
        if self._try_get_container():
            utils.get_logger().error("Container already exists")
            return False

        update_libertine_profile(self._client)

        utils.get_logger().info("Creating container '%s' with distro '%s'" % (self.container_id, self.installed_release))
        create = subprocess.Popen(shlex.split('lxc launch ubuntu-daily:{distro} {id} --profile '
                                              'default --profile libertine'.format(
                                              distro=self.installed_release, id=self.container_id)))
        if create.wait() is not 0:
            utils.get_logger().error("Creating container '{}' failed with code '{}'".format(self.container_id, create.returncode))
            return False

        self._try_get_container()
        _sync_application_dirs_to_host(self._container)
        update_bind_mounts(self._container, self._config, env_home_path())

        self.update_locale()

        username = os.environ['USER']
        uid = str(os.getuid())
        self.run_in_container("userdel -r ubuntu")
        self.run_in_container("useradd -u {} -U -p {} -G sudo,audio,video {}".format(
            uid, crypt.crypt(password or ''), username))
        self.run_in_container("mkdir -p /home/{}".format(username))
        self.run_in_container("chown {0}:{0} /home/{0}".format(username))

        self._create_libertine_user_data_dir()

        _setup_bind_mount_service(self._container, uid, username)

        if multiarch and self.architecture == 'amd64':
            utils.get_logger().info("Adding i386 multiarch support to container '{}'".format(self.container_id))
            self.run_in_container("dpkg --add-architecture i386")

        self.update_packages()

        for package in self.default_packages:
            utils.get_logger().info("Installing package '%s' in container '%s'" % (package, self.container_id))
            if not self.install_package(package, no_dialog=True, update_cache=False):
                utils.get_logger().error("Failure installing '%s' during container creation" % package)
                self.destroy_libertine_container()
                return False

        super().create_libertine_container()

        return True

    def update_packages(self, update_locale=False):
        if not self._timezone_in_sync():
            utils.get_logger().info("Re-syncing timezones")
            self.run_in_container("bash -c 'echo \"%s\" > /etc/timezone'" % self._host_info.get_host_timezone())
            self.run_in_container("rm -f /etc/localtime")
            self.run_in_container("dpkg-reconfigure -f noninteractive tzdata")

        _sync_application_dirs_to_host(self._container)
        update_bind_mounts(self._container, self._config, env_home_path())

        return super().update_packages(update_locale)

    def destroy_libertine_container(self):
        if not self._try_get_container():
            utils.get_logger().error("No such container '%s'" % self.container_id)
            return False

        if not self.stop_container(wait=True):
            utils.get_logger().error("Canceling destruction due to running container")
            return False

        self._container.delete()

        return self._delete_rootfs()

    def _timezone_in_sync(self):
        proc = subprocess.Popen(self._lxc_args('cat /etc/timezone'), stdout=subprocess.PIPE)
        out, err = proc.communicate()
        return out.decode('UTF-8').strip('\n') == self._host_info.get_host_timezone()

    def _lxc_args(self, command, environ={}):
        env_as_args = ' '
        for k, v in environ.items():
            env_as_args += '--env {}="{}" '.format(k, v)

        return shlex.split('lxc exec {}{}-- {}'.format(self.container_id,
                                                       env_as_args,
                                                       command))

    def run_in_container(self, command):
        proc = subprocess.Popen(self._lxc_args(command))
        return proc.wait()

    def start_container(self, home=env_home_path()):
        if not self._try_get_container():
            return False

        if self._manager:
            retries = 0
            while not self._manager.container_operation_start(self.container_id):
                retries += 1
                if retries > 5:
                    return False
                time.sleep(.5)

        if self._container.status == 'Running':
            return True

        requires_remount = self._container.status == 'Stopped'

        if requires_remount:
            update_libertine_profile(self._client)
            update_bind_mounts(self._container, self._config, home)

        if not lxd_start(self._container):
            return False

        if not _wait_for_network(self._container):
            utils.get_logger().warning("Network unavailable in container '{}'".format(self.container_id))

        if requires_remount:
            self.run_in_container("/usr/bin/libertine-lxd-mount-update")

        return True

    def stop_container(self, wait=False):
        if not self._try_get_container():
            return False

        if self._manager:
            if self._manager.container_operation_finished(self.container_id):
                if not lxd_stop(self._container, freeze_on_stop=self._freeze_on_stop):
                    return False
                self._manager.container_stopped(self.container_id)
                return True
            else:
                return False
        else:
            return lxd_stop(self._container, freeze_on_stop=self._freeze_on_stop)

    def restart_container(self, wait=True):
        if not self._try_get_container():
            return False

        if self._container.status != 'Frozen':
            utils.get_logger().warning("Container {} is not frozen. Cannot restart.".format(self._container.name))
            return False

        orig_freeze = self._freeze_on_stop
        self._freeze_on_stop = False

        # We never want to use the manager when restarting.
        self._manager = None

        if not (self.stop_container(wait=True) and self.start_container()):
            return False

        self._freeze_on_stop = orig_freeze
        return self.stop_container(wait=True)
 
    def _get_matchbox_pids(self):
        p = subprocess.Popen(self._lxc_args('pgrep matchbox'), stdout=subprocess.PIPE)
        out, err = p.communicate()
        return out.decode('utf-8').split('\n')

    # FIXME: Remove once window management logic has been moved to the host
    def _start_window_manager(self, args):
        args.extend(self.setup_window_manager())

        if 'matchbox-window-manager' in args:
            pids = self._get_matchbox_pids()

        self._window_manager = psutil.Popen(args)

        time.sleep(.25) # Give matchbox a moment to start in the container
        if self._window_manager.poll() is not None:
            utils.get_logger().warning("Window manager terminated prematurely with exit code '{}'".format(
                                       self._window_manager.returncode))
            self._window_manager = None
        elif 'matchbox-window-manager' in args:
            after_pids = self._get_matchbox_pids()
            if len(after_pids) > len(pids):
                self._matchbox_pid = (set(after_pids) - set(pids)).pop()

    def start_application(self, app_exec_line, environ):
        if not self._try_get_container():
            utils.get_logger().error("Could not get container '{}'".format(self.container_id))
            return None

        if utils.is_snap_environment():
            environ['HOME'] = '/home/{}'.format(environ['USER'])

        if not self.start_container(home=environ['HOME']):
            if self._manager:
                self.lxc_manager_interface.container_stopped(self.container_id)
            return False

        args = self._lxc_args("sudo -E -u {} env PATH={}".format(environ['USER'], environ['PATH']), environ)

        self._start_window_manager(args.copy())

        args.extend(app_exec_line)
        return psutil.Popen(args)

    def finish_application(self, app):
        if self._window_manager is not None:
            utils.terminate_window_manager(self._window_manager)
            self._window_manager = None

            # This is a workaround for an issue on xenial where the process
            # running the window manager is not killed with the application
            if self._matchbox_pid is not None:
                utils.get_logger().debug("Manually killing matchbox-window-manager")
                self.run_in_container("kill -9 {}".format(self._matchbox_pid))
                self._matchbox_pid = None

        app.wait()

        self.stop_container()

    def copy_file_to_container(self, source, dest):
        with open(source, 'rb') as f:
            return self._container.files.put(dest, f.read())

    def delete_file_in_container(self, path):
        return self.run_in_container('rm {}'.format(path))

    def _try_get_container(self):
        if self._container is None:
            self._container = lxd_container(self._client, self.container_id)

        return self._container is not None
