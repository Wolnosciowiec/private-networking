
import subprocess
import psutil
from typing import List, Union
from time import sleep
from threading import RLock
from datetime import date
from .model import Forwarding, HostTunnelDefinitions
from .logger import Logger
from .validation import Validation


class TunnelManager:
    _signatures: List[str]
    _procs: List[subprocess.Popen]
    _starts_history: dict
    _sleep_time = 10
    is_terminating: bool

    def __init__(self):
        self.is_terminating = False
        self._signatures = []
        self._procs = []
        self._lock = RLock(timeout=60)
        self._starts_history = {}

    def get_stats(self, definitions: List[Forwarding]) -> dict:
        definitions_status = {}

        for definition in definitions:
            proc = self._find_process_by_signature(definition.create_ssh_forwarding())
            starts_history = self._starts_history[definition] if definition in self._starts_history else []

            definitions_status[definition] = {
                'pid': proc.pid if proc else '',
                'is_alive': proc is not None,
                'starts_history': starts_history,
                'restarts_count': abs(len(starts_history) - 1)
            }

        return {
            'signatures': self._signatures,
            'status': definitions_status,
            'procs_count': len(self._procs),
            'is_terminating': self.is_terminating
        }

    def spawn_tunnel(self, definition: Forwarding, configuration: HostTunnelDefinitions):
        """
        Threads: Per thread

        :param definition:
        :param configuration:
        :return:
        """

        opts = ''

        if not configuration.ssh_opts:
            if configuration.remote_key:
                opts += ' -i %s' % configuration.remote_key

        signature = definition.create_ssh_forwarding()
        forwarding = opts + ' ' + signature
        Logger.info('Created SSH args: %s' % forwarding)

        with self._lock:
            self._signatures.append(signature)

            if definition not in self._starts_history:
                self._starts_history[definition] = []

            self._starts_history[definition].append(date.today())

        self.spawn_ssh_process(forwarding, definition, configuration, signature)

    def spawn_ssh_process(self, args: str, definition: Forwarding,
                          configuration: HostTunnelDefinitions, signature: str):

        """
        Spawns a SSH process and starts supervising

        Threads: Per thread

        :param args:
        :param definition:
        :param configuration:
        :param signature:
        :return:
        """

        with self._lock:
            self._clean_up()

        if self.is_terminating:
            return

        cmd = ''

        if configuration.remote_password:
            cmd += 'sshpass -p "%s" ' % configuration.remote_password

        cmd += "autossh -M 0 -N -f -o 'PubkeyAuthentication=yes' -o 'PasswordAuthentication=no' -nT %s %s" % (
                args,
                '-p %i %s@%s' % (
                    configuration.remote_port,
                    configuration.remote_user,
                    configuration.remote_host
                )
            )

        Logger.info('Spawning %s' % cmd)
        proc = subprocess.Popen(cmd, shell=True)

        with self._lock:
            self._procs.append(proc)

        sleep(10)

        if not Validation.is_process_alive(signature):
            try:
                stdout, stderr = [proc.stdout.read().decode('utf-8'), proc.stderr.read().decode('utf-8')]
            except:
                stdout, stderr = ['', '']

            Logger.error('Cannot spawn %s, stdout=%s, stderr=%s' % (cmd, stdout, stderr))
            sleep(15)

            return self.spawn_ssh_process(args, definition, configuration, signature)

        Logger.info('Process for "%s" survived initialization, got pid=%i' % (signature, proc.pid))
        self._tunnel_loop(definition, configuration, signature, args)

    def _tunnel_loop(self, definition: Forwarding, configuration: HostTunnelDefinitions, signature: str, args: str):
        """
        One tunnel = one thread of health monitoring and reacting

        Threads: Per thread

        :param definition:
        :param configuration:
        :param signature:
        :param args:
        :return:
        """

        while True:
            if not self._carefully_sleep(definition.validate.interval):
                return

            Logger.debug('Running checks for signature "%s"' % signature)

            if not Validation.is_process_alive(signature):
                Logger.error('The tunnel process exited for signature "%s"' % signature)
                return self.spawn_ssh_process(args, definition, configuration, signature)

            if not Validation.check_tunnel_alive(definition, configuration):
                Logger.error('The health check "%s" failed for signature "%s"' % (
                    definition.validate.method, signature))

                time_to_wait_on_health_check_failure = definition.validate.wait_time_before_restart
                sleep(time_to_wait_on_health_check_failure)

                # check if after given additional short wait time the health is OK
                if time_to_wait_on_health_check_failure and Validation.check_tunnel_alive(definition, configuration):
                    Logger.info('Tunnel "%s" was recovered with restart' % signature)
                    continue

                if definition.validate.kill_existing_tunnel_on_failure:
                    proc = self._find_process_by_signature(signature)

                    if proc:
                        proc.kill()

                return self.spawn_ssh_process(args, definition, configuration, signature)

    def _carefully_sleep(self, sleep_time: int):
        for i in range(0, sleep_time):
            if self.is_terminating:
                Logger.debug('Careful sleep: got termination signal')
                return False

            sleep(1)

        return True

    def close_all_tunnels(self):
        """
        Kill all processes spawned by the TunnelManager

        Threads: Called from main thread
        :return:
        """

        self.is_terminating = True

        for signature in self._signatures:
            proc = self._find_process_by_signature(signature)

            if proc:
                Logger.info('Killing %i (%s)' % (proc.pid, proc.name()))
                proc.kill()

            for proc in psutil.process_iter():
                cmdline = " ".join(proc.cmdline())

                if signature in cmdline:
                    proc.kill()

        for proc in self._procs:
            Logger.info('Killing %i' % proc.pid)
            proc.kill()

    @staticmethod
    def _find_process_by_signature(signature: str) -> Union[psutil.Process, None]:
        for proc in psutil.process_iter():
            cmdline = " ".join(proc.cmdline())

            if signature in cmdline and "autossh" in cmdline:
                return proc

        return None

    def _clean_up(self):
        """ Free up information about processes that no longer are alive,
            so the application will not attempt to kill when gracefully shutting down
        """

        for proc in self._procs.copy():
            Logger.debug('clean_up: Checking if process pid=%i is still alive' % proc.pid)

            if proc.poll() is not None:
                Logger.debug('clean_up: Freeing proc pid=%i' % proc.pid)

                try:
                    self._procs.remove(proc)
                except ValueError:
                    continue

