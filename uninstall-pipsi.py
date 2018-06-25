# -*- coding: utf-8 -*-

# `which pipsi`:
# > /Users/me/.local/bin/pipsi
# `ls -l $(which pipsi)`:
# > /Users/me/.local/venvs/pipsi/bin/pipsi

from __future__ import print_function
import os
import sys
import shutil


PY2 = sys.version_info.major == 2


if PY2:
    from distutils.spawn import find_executable as which
else:
    from shutil import which


def main():
    while True:
        pipsi_path = which('pipsi')
        if not pipsi_path:
            break
        if os.path.islink(pipsi_path):
            pipsi_real = os.path.realpath(pipsi_path)
            venv_path = os.path.dirname(os.path.dirname(pipsi_real))
            hint = ('pipsi real path is {}, suggest venv path is {}\n'
                    'Delete dir {}? (y to confirm) ').format(
                        pipsi_real, venv_path, venv_path)
            r = raw_input(hint)
            if r != 'y':
                print('Deleting file {}, ignore dir {}'.format(pipsi_real, venv_path))
                os.remove(pipsi_real)
            else:
                print('Deleting dir {}'.format(venv_path))
                shutil.rmtree(venv_path)
            print('Deleting symlink {}'.format(pipsi_path))
            os.remove(pipsi_path)
        else:
            # just rm
            print('Deleting file {}'.format(pipsi_path))
            os.remove(pipsi_path)
        print()


if __name__ == '__main__':
    main()
