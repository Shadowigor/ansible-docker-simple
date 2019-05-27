# docker_simple

This is a module for Ansible with the goal to simplify the handling of Docker images.

## Wait, isn't there an official docker module already?

There is! But for me it was quite hard to use and very counterintuitive. For example, it doesn't check, whether an image has to be rebuilt because the `Dockerfile` changed, if 'something' is there, the module was happy. There was also no way to stop a container without knowing _ALL_ of the parameters it was started with. Things like that made me develop this module.

## What it does

It is basically a wrapper for the `docker` command. It does not use the docker API, because the `docker-py` module doesn't support all command line arguments that the `docker` command has.

With the module you are describing the state of a container. It can have the following states

 - `running`: Makes sure the image you are using is up to date, rebuilds/pulls it if needed and then starts a container with the given image and name. The container is recreated and restarted if the image or the run arguments change.
 - `stopped`: Makes sure there is no container running with a given name.
 - `restarted`: Same as `running`, but the container is definitely restarted, even if nothing changed.
 - `built`: Makes sure the image is built, but doesn't start a container.

## How to use it

Use it as a normal docker module with the following parameters:

- `state`: See above
- `name`: The name of the container.
- `path`: The path to the Dockerfile that describes the image of the container. If empty, it is assumed that the image should be pulled form the remote registry.
- `image`: Name of the image. You must not include a tag if you build a local image (`path` is present). The image will automatically get the tag `:local` to more easily distinguish it from pulled images. You can also not use the tag `:local` for images that are supposed to be pulled.
- `command`: The command to run inside the container.
- `build_args`: Command line arguments of the `docker build` command

All other parameters are directly translated to command line arguments of the `docker run` command

## I'd like an example, please

```yaml
- name: Build and start my container
  docker_simple:
    name: mycontainer
    image: myimage
    state: running
    path: /path/to/Dockerfile/of/myimage/
    network: mynetwork
    hostname: thehost
    network_alias: thehost
    env:
    - ENV_VARIABLE=very_useful
    - SOME=thing
    volume:
    - /host/mnt1:/container/mnt1
    - /host/mnt2:/container/mnt2
```

Do a `man docker-run` if you don't know what some of those parameters do that weren't explained above.

Note: A dash (`-`) in an argument name is replace by an underscore (`_`).

## How to install

If you want to use it in a playbook, just copy the `library` folder to the root directory of your playbook. If you want to install it systemwide, add an entry to your `/etc/ansible/ansible.cfg`:

```
library = /usr/share/ansible/library  # Or wherever you want your extra modules
```

And put the contents of the `library` folder there.

---
Copyright (C) 2019 Alain Kohli
