"""
Services related to SONiC images, such as:
1) Download
2) Install
"""

import logging
import subprocess
import os

from host_modules import host_service

MOD_NAME="image_service"

DEFAULT_IMAGE_SAVE_AS="/host/downloaded-sonic"

logger = logging.getLogger(__name__)

class ImageService(host_service.HostModule):
    """DBus endpoint that handles downloading and installing SONiC images
    """

    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='ss', out_signature='is')
    def download(self, image_url, save_as):
        ''' 
        Download a SONiC image.

        Args:
             image_url: url for remote image.
             save_as: local path for the downloaded image
        '''
        logger.info("Download new sonic image from {} as {}".format(image_url, save_as))
        # Create parent directory.
        dir = os.path.dirname(save_as)
        if not os.path.exists(dir):
            os.makedirs(dir)
        cmd = ["/usr/bin/curl", "-Lo", save_as, image_url]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            lines = result.stderr.decode().split('\n')
            for line in lines:
                if 'Error' in line:
                    msg = line
                    break
        return result.returncode, msg


    @host_service.method(host_service.bus_name(MOD_NAME), in_signature='s', out_signature='is')
    def install(self, where):
        '''
        Install a a sonic image:

        Args:
            where: either a local path or a remote url pointing to the image.
        '''
        logger.info("Using sonic-installer to install the image at {}.".format(where))
        cmd = ["sudo", "/usr/local/bin/sonic-installer", "install", "-y", where]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        msg = ''
        if result.returncode:
            lines = result.stderr.decode().split('\n')
            for line in lines:
                if 'Error' in line:
                    msg = line
                    break
        return result.returncode, msg