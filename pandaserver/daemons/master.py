import os
import sys
import pwd
import grp
import signal
import argparse
import logging

import daemon
import lockfile

from pandaserver.config import panda_config, daemon_config
from pandaserver.daemons.utils import END_SIGNALS, DaemonMaster


# get the logger
def get_logger():
    my_logger = logging.getLogger('PanDA-Daemon-Master')
    # remove existing handlers
    while my_logger.hasHandlers():
        my_logger.removeHandler(my_logger.handlers[0])
    # make new handler
    _log_handler = logging.StreamHandler(sys.stdout)
    _log_formatter = logging.Formatter('%(asctime)s %(name)-12s: %(levelname)-8s %(message)s')
    _log_handler.setFormatter(_log_formatter)
    # add new handler
    my_logger.addHandler(_log_handler)
    # debug log level
    my_logger.setLevel(logging.DEBUG)
    # return logger
    return my_logger

# kill the whole process group
def kill_whole():
    os.killpg(os.getpgrp(), signal.SIGKILL)


# main function
def main():
    # whether to run daemons
    if not getattr(daemon_config, 'enable', False):
        return
    # get logger
    main_log = get_logger()
    # parse option
    parser = argparse.ArgumentParser()
    parser.add_argument('-P', '--pidfile', action='store', dest='pidfile',
                        default=None, help='pid filename')
    options = parser.parse_args()
    uname = getattr(daemon_config, 'uname', 'nobody')
    gname = getattr(daemon_config, 'gname', 'nobody')
    uid = pwd.getpwnam(uname).pw_uid
    gid = grp.getgrnam(gname).gr_gid
    n_workers = getattr(daemon_config, 'n_proc', 1)
    main_log.info('main start')
    # daemon context
    dc = daemon.DaemonContext(  stdout=sys.stdout, stderr=sys.stderr,
                                uid=uid, gid=gid,
                                pidfile=lockfile.FileLock(options.pidfile))
    with dc:
        # get logger inside daemon context
        tmp_log = get_logger()
        # record in PID file
        with open(options.pidfile, 'w') as pid_file:
            pid_file.write('{0}'.format(os.getpid()))
        # master object
        master = DaemonMaster(logger=tmp_log, n_workers=n_workers)
        # function to end master when end signal caught
        def end_master(sig, frame):
            tmp_log.info('got end signal: {sig}'.format(sig=sig))
            master.stop()
            kill_whole()
        # set signal handler
        for sig in END_SIGNALS:
            signal.signal(sig, end_master)
        # start master
        master.run()
    # get logger again
    main_log = get_logger()
    main_log.info('main end')


# run
if __name__ == '__main__':
    main()