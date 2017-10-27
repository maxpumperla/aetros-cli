from __future__ import print_function, division
from __future__ import absolute_import

import json
import os
import subprocess
import sys
import six

from aetros.logger import GeneralLogger
from aetros.utils import unpack_full_job_id, read_config
from .backend import JobBackend
from .Trainer import Trainer


class GitCommandException(Exception):
    cmd = None


def start(logger, full_id, fetch=True):
    """
    Starts the training process with all logging of a job_id
    """

    owner, name, id = unpack_full_job_id(full_id)

    if isinstance(sys.stdout, GeneralLogger):
        # we don't want to have stuff written to stdout before in job's log
        sys.stdout.clear_buffer()

    job_backend = JobBackend(model_name=owner + '/' + name)
    job_backend.section('checkout')

    if fetch:
        job_backend.fetch(id)

    job_backend.restart(id)
    job_backend.start()

    if job_backend.is_simple_model():
        start_keras(logger, job_backend)
    else:
        start_custom(logger, job_backend)


def start_custom(logger, job_backend):
    job_model = job_backend.get_job_model()

    work_tree = job_backend.git.work_tree

    my_env = os.environ.copy()
    if 'PYTHONPATH' not in my_env:
        my_env['PYTHONPATH'] = ''

    my_env['PYTHONPATH'] += ':' + os.getcwd()
    my_env['AETROS_MODEL_NAME'] = job_backend.model_name
    my_env['AETROS_JOB_ID'] = job_backend.job_id
    my_env['AETROS_ATTY'] = '1'
    my_env['AETROS_GIT'] = job_backend.git.get_base_command()

    project_config = read_config(work_tree + '/.aetros.yml')

    job_config = job_backend.job['config']

    if 'command' not in job_config and ('command' not in project_config or project_config['command'] == ''):
        logger.error('No "command" specified in .aetros.yml file. See Configuration section in the documentation.')
        sys.exit(1)

    if 'command' in job_config:
        command = job_config['command']
    else:
        command = project_config['command']

    image = None
    if 'image' in job_config:
        image = job_config['image']

    logger.info("Switch working directory to " + work_tree)
    os.chdir(job_backend.git.work_tree)

    if image is not None:
        job_backend.set_system_info('image/name', str(image))

        if project_config['dockerfile'] or project_config['install']:
            dockerfile = project_config['dockerfile']

            if not (isinstance(dockerfile, six.string_types) and os.path.exists(dockerfile)):
                if isinstance(dockerfile, six.string_types):
                    dockerfile_content = dockerfile
                elif isinstance(dockerfile, list) and len(dockerfile) > 0:
                    if not dockerfile[0].startswith('FROM '):
                        dockerfile.insert('FROM ' + image, 0)

                    dockerfile_content = "\n".join(dockerfile)
                else:
                    dockerfile_content = 'FROM ' + image + '\nRUN '

                    if isinstance(project_config['install'], list):
                        dockerfile_content += '\n RUN '.join(project_config['install'])
                    else:
                        dockerfile_content += project_config['install']

                dockerfile_content = '# CREATED BY AETROS because of "install" or "dockerfile" config in .aetros.yml.\n' \
                                     + dockerfile_content

                with open('Dockerfile', 'w') as f:
                    f.write(dockerfile_content)

                dockerfile = 'Dockerfile'
                job_backend.commit_file('Dockerfile')

            docker_build = [
                project_config['docker'],
                'build',
                '-t', job_backend.model_name,
                '-f', dockerfile,
                '.',
            ]

            logger.info("Prepare docker image: $ " + (' '.join(docker_build)))

            p = execute_command(args=docker_build, bufsize=1, stderr=subprocess.PIPE, stdout=subprocess.PIPE)

            if p.returncode:
                job_backend.fail('Image build error')
                sys.exit(p.returncode)

            image = job_backend.model_name

        logger.info("Pull docker image: $ " + image)
        execute_command(args=[project_config['docker'], 'pull', image], bufsize=1,
            stderr=subprocess.PIPE, stdout=subprocess.PIPE)

        inspections = execute_command_stdout([project_config['docker'], 'inspect', image])
        inspections = json.loads(inspections.decode('utf-8'))
        if inspections:
            inspection = inspections[0]
            with job_backend.git.batch_commit('Docker image'):
                job_backend.set_system_info('image/id', inspection['Id'])
                job_backend.set_system_info('image/docker_version', inspection['DockerVersion'])
                job_backend.set_system_info('image/created', inspection['Created'])
                job_backend.set_system_info('image/container', inspection['Container'])
                job_backend.set_system_info('image/architecture', inspection['Architecture'])
                job_backend.set_system_info('image/os', inspection['Os'])
                job_backend.set_system_info('image/size', inspection['Size'])
                job_backend.set_system_info('image/rootfs', inspection['RootFS'])

        # make sure old container is removed
        subprocess.call([project_config['docker'], 'rm', job_backend.job_id])

        docker_command = [project_config['docker'], 'run', '--name', job_backend.job_id]
        docker_command += project_config['docker_options']
        docker_command += ['--mount', 'type=bind,source='+job_backend.git.work_tree+',destination=/exp']
        docker_command += ['-w', '/exp']

        if 'resources' in job_backend.job:
            assigned_resources = job_backend.job['resources']
            # todo, limit the docker container
            # todo, assign GPU to nvidia docker and set env for tensorflow

        docker_command.append(image)

        if isinstance(command, list):
            docker_command += command
        else:
            docker_command += ['sh', '-c', command]

        command = docker_command

    if not isinstance(command, list):
        command = ['sh', '-c', command]

    logger.warning("$ %s " % str(command))

    p = None
    wait_stdout = None
    wait_stderr = None
    try:
        job_backend.section('command')
        p = subprocess.Popen(args=command, bufsize=1, stderr=subprocess.PIPE, stdout=subprocess.PIPE, env=my_env)
        wait_stdout = sys.stdout.attach(p.stdout)
        wait_stderr = sys.stderr.attach(p.stderr)

        p.wait()
        wait_stdout()
        wait_stderr()

        job_backend.set_system_info('exit_code', p.returncode)

        if p.returncode:
            job_backend.fail()

        sys.exit(p.returncode)
    except KeyboardInterrupt:
        # We can not send a SIGINT to the child process
        # as we don't know whether it received it already (pressing CTRL+C) or not (sending SIGINT to this process only
        # instead of to the group), so we assume it received it. A second signal would force the exit.

        if p and p.poll() is None:
            p.wait()
            if wait_stdout: wait_stdout()
            if wait_stderr: wait_stderr()

        # check if there was a JobBackend in the command
        # if so, we do not add any further stuff to the git
        if job_backend.git.has_file('aetros/job/status/progress.json'):
            # make sure, we do not overwrite their stuff
            job_backend.stop()
        else:
            logger.warning("Job aborted.")
            job_backend.abort()


def execute_command_stdout(command, input=None):
    p = subprocess.Popen(command, bufsize=1, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    out, err = p.communicate(input)

    if p.returncode:
        sys.stderr.write(out)
        sys.stderr.write(err)
        raise Exception('Could not execute command: ' + command)

    return out


def execute_command(**kwargs):
    p = subprocess.Popen(**kwargs)
    wait_stdout = sys.stdout.attach(p.stdout)
    wait_stderr = sys.stderr.attach(p.stderr)

    p.wait()
    wait_stdout()
    wait_stderr()

    return p


def git_execute(logger, repo_path, args):
    args = ['git', '--git-dir', repo_path + '/.git', '--work-tree', repo_path] + args
    logger.info("$ %s" % (' '.join(args), ))

    p = execute_command(args=args, bufsize=1, stderr=subprocess.PIPE, stdout=subprocess.PIPE)

    if p.returncode != 0:
        exception = GitCommandException("Git command returned not 0. " + (' '.join(args)))
        exception.cmd = (' '.join(args))
        raise exception


def start_keras(logger, job_backend):
    if 'KERAS_BACKEND' not in os.environ:
        os.environ['KERAS_BACKEND'] = 'tensorflow'

    from . import keras_model_utils

    # we need to import keras here, so we know which backend is used (and whether GPU is used)
    os.chdir(job_backend.git.work_tree)
    logger.debug("Start simple model")

    # we use the source from the job commit directly
    with job_backend.git.batch_commit('Git Version'):
        job_backend.set_system_info('git_remote_url', job_backend.git.get_remote_url('origin'))
        job_backend.set_system_info('git_version', job_backend.git.job_id)

    # all our shapes are Tensorflow schema. (height, width, channels)
    import keras.backend
    if hasattr(keras.backend, 'set_image_dim_ordering'):
        keras.backend.set_image_dim_ordering('tf')

    if hasattr(keras.backend, 'set_image_data_format'):
        keras.backend.set_image_data_format('channels_last')

    from .KerasCallback import KerasCallback
    trainer = Trainer(job_backend)
    keras_logger = KerasCallback(job_backend, job_backend.logger)

    try:
        job_backend.progress(0, job_backend.job['config']['epochs'])

        logger.info("Start training")
        keras_model_utils.job_start(job_backend, trainer, keras_logger)

        job_backend.done()
    except KeyboardInterrupt:
        logger.warning("Aborted.")
        sys.exit(1)