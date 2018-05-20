#!/usr/bin/env python

# Copyright (C) 2018 Alain Kohli
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""
This is an ansible module that provides a somewhat more simple interface for
docker. You define the state of a container and the image for it will be
pulled/built if needed. The module arguments translate 1:1 to command line
arguments for the 'docker run' command, with a few exceptions:

state: running   - The container image is up to date and the container is running
       restarted - The container image is up to date and the container is started (restarted
                   if it was already running)
       stopped   - The container is not running

name: The name of the container.

path: The path to the Dockerfile that describes the image of the container. If
      empty, it is assumed that the image should be pulled form the remote
      registry.

image: Name of the image. You must not include a tag if you build a local image
       ('path' is present). The image will automatically get the tag ':local'
       to more easily distinguish it from pulled images. You can also not use
       the tag ':local' for images that are supposed to be pulled.

command: The command to run inside the container.

Only 'state' and 'name' are always required. 'image' is not required if 'state'
equals 'stopped', otherwise it is required as well. For the other arguments,
limitations of the 'docker' command may apply.

Note that module arguments are translated to the long names of the command line
arguments and that you have to substitute a '-' in the middle of the argument
name with a '_'.

ALL docker command line arguments are supported, with the exception of some
useless ones for ansible (--interactive for example). If an argument is not
supported, it is most likely very new or got simply overlooked. Or it is not
documented in the official manpage.
"""

from __future__ import print_function
import os
import datetime
import distutils.dir_util
from distutils.errors import DistutilsFileError
from subprocess import check_output as exec_command
from subprocess import CalledProcessError
from six import iteritems
from ansible.module_utils.basic import AnsibleModule

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'community'
}

DOCKER_COMMANDS_PATH = '/var/local/ansible/docker_simple'


def fail(module, msg):
    """
    Notify ansible that the module execution failed.

    :param module: Interface to ansible.
    :param msg: The error message to display.
    :return: A dict indicating that something changed.
    """

    module.fail_json(msg=msg, changed=True)
    return dict(changed=True)


class Container:
    """
    Represents a docker container that you can start, stop, etc.
    """

    class InvalidArgumentException(Exception):
        """
        Raised if you pass an illegal argument combination to the module.
        """

        pass

    def __init__(self, **kwargs):
        """
        Initializes all commands and facts the container might need.

        :param kwargs: All command line arguments for the docker commands.
        """

        # Nothing changed on the remote host by default
        self.changed = False

        # Very helpful for debugging
        self.change_reason = []

        # We need those values in a couple of places
        # 'name' is guaranteed to be present by ansible
        self.name = kwargs['name']
        self.image = kwargs.pop('image', None)
        self.path = kwargs.pop('path', None)

        # Local and remote images are treated slightly differently
        if self.path:
            # We do this to more easily distinguish locally built and pulled images
            if ':' in self.image:
                raise Container.InvalidArgumentException("No tags are allowed when building a local image")
            self.image += ':local'
            self.is_local_image = True
        else:
            if self.image[:-6] == ':local':
                raise Container.InvalidArgumentException("The 'local' tag is reserved for locally built images")
            self.is_local_image = False

        # We need to be able to see, when the arguments of the module change
        # from one run to another, because that changes the behavior of e.g.
        # restart. We do that by saving the commands that were used for
        # starting the current sessions of the containers somewhere.
        distutils.dir_util.mkpath(DOCKER_COMMANDS_PATH, mode=0o600)
        # With the mode 'r+', the file isn't created if it doesn't exist
        try:
            self.prev_commands_file = open(DOCKER_COMMANDS_PATH + '/' + self.name, 'r+')
        except IOError:
            self.prev_commands_file = open(DOCKER_COMMANDS_PATH + '/' + self.name, 'w+')
        self.prev_build_command = self.prev_commands_file.readline()
        self.prev_run_command = self.prev_commands_file.readline()

        # Strip the newlines at the end
        if self.prev_build_command and self.prev_build_command[-1] == '\n':
            self.prev_build_command = self.prev_build_command[:-1]
        if self.prev_run_command and self.prev_run_command[-1] == '\n':
            self.prev_run_command = self.prev_run_command[:-1]

        # Construct the docker commands that we might have to execute
        self.build_command = self._construct_docker_build_command(self.image, **kwargs)
        self.run_command = self._construct_docker_run_command(self.image, **kwargs)

        # We also need the string version in some places
        self.build_command_str = ' '.join(self.build_command)
        self.run_command_str = ' '.join(self.run_command)

    def __del__(self):
        """
        Save the current build and run commands to a file if the container is
        running now.
        """

        # If the state is 'stopped', the 'image' argument doesn't have to be
        # passed, thus the commands will be None. We don't want to save that,
        # because if the container is started again with the same arguments
        # as before it was stopped, we don't have to recreate the container.
        if self.run_command and self.build_command:
            # In case we opened the file in 'r+' mode and read it first
            self.prev_commands_file.seek(0)
            self.prev_commands_file.write(self.build_command_str + '\n' +
                                          self.run_command_str)
            self.prev_commands_file.truncate()
        self.prev_commands_file.close()

    def ensure_running(self):
        """
        Make sure the container is running. If the image is outdated, it is
        rebuilt first and then the container is restarted.
        """

        self.ensure_image_is_updated()

        if self.run_command_str != self.prev_run_command:
            self.change_reason.append("Arguments changed for run command")

        runs = self.running()
        if runs:
            if self.changed or self.run_command_str != self.prev_run_command:
                self.stop()
                self.remove()
                self.run()
        elif runs is None:
            self.run()
        else:
            if self.changed or self.run_command_str != self.prev_run_command:
                self.remove()
                self.run()
            else:
                self.start()

    def ensure_stopped(self):
        """
        Make sure that the container is stopped.
        """

        if self.running():
            self.stop()

    def ensure_restarted(self):
        """
        Make sure the container is running and restart it if it already is.
        """

        runs = self.running()
        if runs:
            self.restart()
        elif runs is None:
            self.run()
        else:
            # If the image changed, just issuing a restart won't be enough
            if self.changed:
                self.remove()
                self.run()
            else:
                self.start()

    def ensure_image_is_updated(self):
        """
        Make sure the image of the container is up to date and rebuild it if
        necessary.
        """

        if self.is_local_image:
            if self.needs_rebuild():
                self.build()
        else:
            if self.needs_pull():
                self.pull()

    def needs_rebuild(self):
        """
        Check, whether the image needs to be rebuilt. This function looks at
        the path to the Dockerfile and checks, whether any files there are
        newer than the docker image.

        :return: True if the image needs to be rebuilt, False otherwise.
        """

        # If the build command changed, we definitely have to rebuild it
        if self.build_command_str != self.prev_build_command:
            self.change_reason.append("Arguments changed for build command")
            return True

        # Get the creation time of the docker image
        try:
            time_str = exec_command(['docker', 'inspect', '--format', '{{.Created}}', self.image])
        except CalledProcessError:
            # If that command fails, we assume that the image was not found
            self.change_reason.append("Image not found, needs rebuild")
            return True

        # Convert the time to a format we can use for comparisons
        image_creation_time = datetime.datetime.strptime(time_str[:26], "%Y-%m-%dT%H:%M:%S.%f")

        # Iterate over all files in the image path to see if any of those files
        # were more recently modified than the image creation time.
        for root, subdirs, files in os.walk(self.path):
            for filename in files:
                file_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(os.path.join(root, filename)))
                if image_creation_time < file_mtime:
                    self.change_reason.append("File changed: " + filename)
                    return True
        return False

    def needs_pull(self):
        """
        Check, whether the image for the container already exists locally or
        if it has to be pulled first. This function can currently not check,
        if the local version is also the newest one, it only checks whether
        it exists locally or not.

        :return: True if it needs to be pulled and doesn't exist locally, False otherwise.
        """

        try:
            return not exec_command(['docker', 'inspect', '--format', '{{.ID}}', self.image])
        except CalledProcessError:
            # If that command fails, we assume that the image was not found locally
            self.change_reason.append("Image not found, needs pull")
            return True

    def running(self):
        """
        Check, whether the container is running or not.

        :return: True if it is running, False otherwise.
        """

        try:
            # This will either be 'true' (container runs), 'false' (container
            # is stopped) or throw an exception (container doesn't exist).
            return 'true' in exec_command(['docker', 'inspect', '--format', '"{{.State.Running}}"', self.name])
        except CalledProcessError as e:
            # If that command fails, the container is not just not running, but it also doesn't exist
            return None

    def run(self):
        """
        Run the container.
        """

        exec_command(self.run_command)
        self.change_reason.append("Executed 'docker run'")
        self.changed = True

    def start(self):
        """
        Starts an existing container.
        """

        self.change_reason.append("Executed 'docker start'")
        exec_command(['docker', 'start', self.name])
        self.changed = True

    def restart(self):
        """
        Restart the docker container (or start it if it wasn't running).
        """

        if self.changed:
            self.stop()
            self.remove()
            self.run()
        else:
            self.change_reason.append("Executed 'docker restart'")
            exec_command(['docker', 'restart', self.name])
            self.changed = True

    def stop(self):
        """
        Stop the docker container.
        """

        self.change_reason.append("Executed 'docker stop'")
        exec_command(['docker', 'stop', self.name])
        self.changed = True

    def remove(self):
        """
        Remove the docker container.
        """

        self.change_reason.append("Executed 'docker rm'")
        exec_command(['docker', 'rm', self.name])
        self.changed = True

    def build(self):
        """
        Build the docker image of the container.
        """

        # Makes sure this runs in the directory where the Dockerfile is,
        # otherwise the command would be wrong.
        exec_command(self.build_command, cwd=self.path)
        self.change_reason.append("Executed 'docker build'")
        self.changed = True

    def pull(self):
        """
        Pull the image of the container from the registry.
        """

        exec_command(['docker', 'pull', self.image])
        self.change_reason.append("Executed 'docker pull'")
        self.changed = True

    @staticmethod
    def _construct_docker_build_command(image, **kwargs):
        """
        Construct the docker command to build an image.

        :param image: The image name
        :param kwargs: The command line arguments for the 'build' command.
        :return: The finished command.
        """

        # There is a special argument that holds all build options. You should
        # rarely need build options, so it's ok to have them as a subsection.
        # All other options are for the 'run' command.
        build_args = kwargs.pop('build_args', dict())

        # I'm not sure why this is necessary, but it is
        if build_args is None:
            build_args = dict()

        # We need to specify to name of the image that is being built. This is
        # a special argument that is used by the 'run' command as well.
        build_args['tag'] = image

        # Construct the build command from the bulid_args
        build_command = Container._construct_docker_command('build', **build_args)

        # We use this because in some cases, the creation time is not updated
        # otherwise
        build_command.append("--no-cache")

        # The last argument is the path to the Dockerfile. We will make sure
        # that we change to the directory where the Dockerfile resides first
        # to avoid copying unnecessary files to the build context.
        build_command.append('.')

        return build_command

    @staticmethod
    def _construct_docker_run_command(image, **kwargs):
        """
        Construct the docker command to run a container.

        :param image: The image name of the container
        :param kwargs: The command line arguments for the 'run' command.
        :return: The finished command.
        """

        # There is no command line argument called 'command', the command you
        # want to execute is just the last command line argument you pass. So
        # we have to take it out before we build the run command, otherwise
        # we will have an unknown argument.
        command = kwargs.pop('command')

        # Construct the run command from the kwargs
        run_command = Container._construct_docker_command('run', **kwargs)

        # We want to run it in the background
        run_command.append('-d')

        # These arguments don't have a name and have to be last
        run_command.append(image)
        if command:
            run_command.append(command)

        return run_command

    @staticmethod
    def _construct_docker_command(command, **kwargs):
        """
        Constructs a docker command based on the command name and keyword
        arguments that represent the command line arguments.

        :param command: The docker command (build, run, ...)
        :param kwargs: The command line arguments for the docker command
                       (e.g. net_alias: foo -> --net-alias foo)
        :return: The finished command.
        """

        # Docker commands always start like this
        command = ['docker', command]

        # Iterate over every argument. The key is also the name of the command
        # line argument for the docker command.
        for key, value in iteritems(kwargs):
            # To have a valid command line argument name, we have to alter the key a bit
            arg_name = '--' + key.replace('_', '-')

            if isinstance(value, list):
                # Add a separate command line argument for each element in a list
                for list_item in value:
                    command.extend([arg_name, str(list_item)])
            # It might happen that the value is None
            elif value:
                # It might be a number that has to be converted to a string first
                command.extend([arg_name, str(value)])
        return command


def run_module():
    """
    Main function that get executed every time the module is used in a
    playbook.

    :return: A JSON string indicating whether something changed on the remote
             host or not.
    """

    # Define the available arguments/parameters that a user can pass to the module
    module_args = dict(
        state=dict(type='str', required=True, default=None, choices=['running', 'stopped', 'restarted']),
        name=dict(type='str', required=True, default=None),
        image=dict(type='str', required=False, default=None),
        path=dict(type='str', required=False, default=None),
        command=dict(type='str', required=False, default=None),
        build_args=dict(type='dict', required=False, default=None),
        add_host=dict(type='list', required=False, default=None),
        blkio_weight=dict(type='int', required=False, default=None),
        blkio_weight_device=dict(type='str', required=False, default=None),
        cpu_shares=dict(type='int', required=False, default=None),
        cap_add=dict(type='list', required=False, default=None),
        cap_drop=dict(type='list', required=False, default=None),
        cgroup_parent=dict(type='str', required=False, default=None),
        cidfile=dict(type='str', required=False, default=None),
        cpu_count=dict(type='int', required=False, default=None),
        cpu_percent=dict(type='int', required=False, default=None),
        cpu_period=dict(type='int', required=False, default=None),
        cpuset_cpus=dict(type='str', required=False, default=None),
        cpuset_mems=dict(type='str', required=False, default=None),
        cpu_quota=dict(type='int', required=False, default=None),
        cpu_rt_period=dict(type='int', required=False, default=None),
        cpu_rt_runtime=dict(type='int', required=False, default=None),
        cpus=dict(type='str', required=False, default=None),
        device=dict(type='list', required=False, default=None),
        device_cgroup_rule=dict(type='list', required=False, default=None),
        device_read_bps=dict(type='list', required=False, default=None),
        device_read_iops=dict(type='list', required=False, default=None),
        device_write_bps=dict(type='list', required=False, default=None),
        device_write_iops=dict(type='list', required=False, default=None),
        dns_search=dict(type='list', required=False, default=None),
        dns_option=dict(type='list', required=False, default=None),
        dns=dict(type='list', required=False, default=None),
        env=dict(type='list', required=False, default=None),
        entrypoint=dict(type='list', required=False, default=None),
        env_file=dict(type='list', required=False, default=None),
        expose=dict(type='list', required=False, default=None),
        group_add=dict(type='list', required=False, default=None),
        hostname=dict(type='str', required=False, default=None),
        ip=dict(type='str', required=False, default=None),
        ip6=dict(type='str', required=False, default=None),
        ipc=dict(type='str', required=False, default=None),
        isolation=dict(type='str', required=False, default=None),
        label=dict(type='list', required=False, default=None),
        kernel_memory=dict(type='str', required=False, default=None),
        label_file=dict(type='list', required=False, default=None),
        link=dict(type='list', required=False, default=None),
        link_local_ip=dict(type='list', required=False, default=None),
        log_driver=dict(type='str', required=False, default=None),
        log_opt=dict(type='list', required=False, default=None),
        memory=dict(type='str', required=False, default=None),
        memory_reservation=dict(type='str', required=False, default=None),
        memory_swap=dict(type='str', required=False, default=None),
        mac_address=dict(type='str', required=False, default=None),
        mount=dict(type='list', required=False, default=None),
        network=dict(type='str', required=False, default=None),
        network_alias=dict(type='list', required=False, default=None),
        oom_kill_disable=dict(type='str', required=False, default=None),
        oom_score_adj=dict(type='str', required=False, default=None),
        publish_all=dict(type='str', required=False, default=None),
        publish=dict(type='list', required=False, default=None),
        pid=dict(type='str', required=False, default=None),
        userns=dict(type='str', required=False, default=None),
        pids_limit=dict(type='str', required=False, default=None),
        uts=dict(type='str', required=False, default=None),
        privileged=dict(type='str', required=False, default=None),
        read_only=dict(type='str', required=False, default=None),
        restart=dict(type='str', required=False, default=None),
        rm=dict(type='str', required=False, default=None),
        security_opt=dict(type='list', required=False, default=None),
        storage_opt=dict(type='list', required=False, default=None),
        stop_signal=dict(type='str', required=False, default=None),
        stop_timeout=dict(type='str', required=False, default=None),
        shm_size=dict(type='str', required=False, default=None),
        sysctl=dict(type='str', required=False, default=None),
        sig_proxy=dict(type='str', required=False, default=None),
        memory_swappiness=dict(type='str', required=False, default=None),
        tty=dict(type='str', required=False, default=None),
        tmpfs=dict(type='list', required=False, default=None),
        user=dict(type='str', required=False, default=None),
        ulimit=dict(type='list', required=False, default=None),
        volume=dict(type='list', required=False, default=None),
        volume_driver=dict(type='str', required=False, default=None),
        volumes_from=dict(type='list', required=False, default=None),
        workdir=dict(type='str', required=False, default=None)
    )

    # Create an object to communicate with ansible
    module = AnsibleModule(argument_spec=module_args, supports_check_mode=True)

    # The 'state' is a special argument that is handled by this function and
    # not the Container class.
    state = module.params.pop('state')

    # Make sure all arguments that have to be present are here (some of this
    # is already handled by ansible).
    if state != 'stopped' and 'image' not in module.params:
        return fail(module, 'Invalid argument: No image name provided')

    # Create a Container class
    try:
        container = Container(**module.params)
    except Container.InvalidArgumentException as e:
        return fail(module, 'Invalid argument: ' + str(e))
    except OSError as e:
        return fail(module, 'Failed to open file to store previous docker commands: ' + str(e))
    except DistutilsFileError as e:
        return fail(module, 'Failed to create path to store previous docker commands: ' + str(e))

    # Ensure the container is in the desired state
    try:
        if state == 'running':
            container.ensure_running()
        elif state == 'restarted':
            container.ensure_restarted()
        elif state == 'stopped':
            container.ensure_stopped()
    except CalledProcessError as e:
        return fail(module, 'Docker command failed: ' + ' '.join(e.cmd) + '\n\n' + e.output)

    # Inform ansible that we were successful and whether something changed on
    # the remote host or not.
    result = dict(changed=container.changed,
                  change_reason=container.change_reason)
    module.exit_json(**result)
    return result


def main():
    run_module()


if __name__ == '__main__':
    main()
