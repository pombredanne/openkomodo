
"""Apply these patches to the Mozilla Mercurial checkout."""

import sys

def applicable(config):
    return config.mozVer == 18.0 and \
           config.patch_target == "mozilla" and \
           sys.platform.startswith('linux')

def patch_args(config):
    # use -p1 to better match hg patches
    return ['-p1']

