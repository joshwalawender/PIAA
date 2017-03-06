import logging
import os
import shutil
import subprocess

from collections import namedtuple
from glob import glob

from astropy.io import fits
from astropy.nddata.utils import Cutout2D
from astropy.stats import sigma_clipped_stats
from astropy.table import Table
from astropy.utils.console import ProgressBar
from astropy.visualization import SqrtStretch
from astropy.visualization.mpl_normalize import ImageNormalize
from astropy.wcs import WCS

from photutils import Background2D
from photutils import MedianBackground
from photutils import RectangularAperture
from photutils import SigmaClip
from photutils import aperture_photometry
from photutils import make_source_mask

import h5py
import numpy as np
import pandas as pd

from matplotlib import gridspec
from matplotlib import patches
from matplotlib import pyplot as plt

from . import utils


Stamp = namedtuple('Stamp', ['row_slice', 'col_slice', 'mid_point', 'cutout'])


class Observation(object):

    def __init__(self, image_dir, aperture_size=6, camera_bias=1024, log_level='INFO', *args, **kwargs):
        """ A sequence of images to be processed as one observation """
        assert os.path.exists(image_dir), "Specified directory does not exist"

        log_level = getattr(logging, log_level, 'INFO')

        logging.basicConfig(filename='{}/piaa.log'.format(image_dir), level=log_level)
        self.logger = logging
        self.logger.info('*' * 80)
        self.logger.info('Setting up Observation for analysis')

        super(Observation, self).__init__()

        if image_dir.endswith('/'):
            image_dir = image_dir[:-1]
        self._image_dir = image_dir

        self.camera_bias = camera_bias

        self._img_h = 3476
        self._img_w = 5208

        # Background estimation boxes
        self.background_box_h = 316
        self.background_box_w = 434
        self.background_estimates = dict()

        self.background_region = {}

        self.aperture_size = aperture_size
        self.stamp_size = (None, None)

        self._point_sources = None
        self._pixel_locations = None

        self.rgb_masks = None  # These are trimmed, see `subtract_background`

        self._stamp_masks = (None, None, None)
        self._stamps_cache = {}

        self._hdf5 = h5py.File(image_dir + '.hdf5')
        self._hdf5_subtracted = h5py.File(image_dir + '_subtracted.hdf5')

        self._load_images()

    @property
    def point_sources(self):
        if self._point_sources is None:
            self.lookup_point_sources()

        return self._point_sources

    @property
    def image_dir(self):
        """ Image directory containing FITS files

        When setting a new image directory, FITS files are automatically
        loaded into the `files` property

        Returns:
            str: Path to image directory
        """
        return self._image_dir

    @image_dir.setter
    def image_dir(self, directory):
        self._load_images()

    @property
    def pixel_locations(self):
        if self._pixel_locations is None:
            # Get RA/Dec coordinates from first frame
            ra = self.point_sources['ALPHA_J2000']
            dec = self.point_sources['DELTA_J2000']

            locs = list()

            for f in self.files:
                wcs = WCS(f)
                xy = np.array(wcs.all_world2pix(ra, dec, 1, ra_dec_order=True))

                # Transpose
                locs.append(xy.T)

            locs = np.array(locs)
            self._pixel_locations = pd.Panel(locs)

        return self._pixel_locations

    @property
    def stamps(self):
        return self._stamps_cache

    # @property
    # def num_frames(self):
    #     assert self.psc_collection is not None
    #     return self.psc_collection.shape[0]

    # @property
    # def num_stars(self):
    #     assert self.psc_collection is not None
    #     return self.psc_collection.shape[1]

    @property
    def data_cube(self):
        try:
            cube_dset = self._hdf5['cube']
        except KeyError:
            self.logger.debug("Creating data cube")
            cube_dset = self._hdf5.create_dataset('cube', (len(self.files), self._img_h, self._img_w))
            for i, f in enumerate(self.files):
                cube_dset[i] = fits.getdata(f) - self.camera_bias

        return cube_dset

    def subtract_frame_background(self, stamp, frame_index,
                                  r_mask=None, g_mask=None, b_mask=None, mid_point=None,
                                  background_sub_method='median', store_background=False):
        """ Perform RGB background subtraction

        Args:
            stamp (numpy.array): A stamp of the data
            r_mask (numpy.ma.array, optional): A mask of the R channel
            g_mask (numpy.ma.array, optional): A mask of the G channel
            b_mask (numpy.ma.array, optional): A mask of the B channel
            background_sub_method (str, optional): Subtraction method of `median` or `mean`

        Returns:
            numpy.array: The background subtracted data recomined into one array
        """
        # self.logger.debug("Subtracting background - {}".format(background_sub_method))

        background_region_id = (int(mid_point[1] // self._back_w), int(mid_point[0] // self._back_h))
        self.logger.debug("Background region: {}\tFrame: {}".format(background_region_id, frame_index))

        try:
            frame_background = self.background_region[frame_index]
        except KeyError:
            frame_background = dict()
            self.background_region[frame_index] = frame_background

        try:
            background_region = frame_background[background_region_id]
        except KeyError:
            background_region = dict()
            self.background_region[frame_index][background_region_id] = background_region

        try:
            r_channel_background = background_region['red']
            g_channel_background = background_region['green']
            b_channel_background = background_region['blue']
        except KeyError:
            r_channel_background = list()
            g_channel_background = list()
            b_channel_background = list()
            if store_background:
                self.background_region[frame_index][background_region_id]['red'] = r_channel_background
                self.background_region[frame_index][background_region_id]['green'] = g_channel_background
                self.background_region[frame_index][background_region_id]['blue'] = b_channel_background

        self.logger.debug("R channel background {}".format(r_channel_background))
        self.logger.debug("G channel background {}".format(g_channel_background))
        self.logger.debug("B channel background {}".format(b_channel_background))

        if len(r_channel_background) < 5:

            self.logger.debug("Getting source mask {} {} {}".format(type(stamp), stamp.dtype, stamp.shape))
            source_mask = make_source_mask(stamp, snr=3., npixels=2)

            if r_mask is None or g_mask is None or b_mask is None:
                self.logger.debug("Making RGB masks for data subtraction")
                self._stamp_masks = utils.make_masks(stamp)
                r_mask, g_mask, b_mask = self._stamp_masks

            self.logger.debug("Determining backgrounds")
            r_masked_data = np.ma.array(stamp, mask=np.logical_or(source_mask, ~r_mask))
            r_stats = sigma_clipped_stats(r_masked_data, sigma=3.)
            r_channel_background.append(r_stats)

            g_masked_data = np.ma.array(stamp, mask=np.logical_or(source_mask, ~g_mask))
            g_stats = sigma_clipped_stats(g_masked_data, sigma=3.)
            g_channel_background.append(g_stats)

            b_masked_data = np.ma.array(stamp, mask=np.logical_or(source_mask, ~b_mask))
            b_stats = sigma_clipped_stats(b_masked_data, sigma=3.)
            b_channel_background.append(b_stats)

        method_lookup = {
            'mean': 0,
            'median': 1,
        }
        method_idx = method_lookup[background_sub_method]

        self.logger.debug("Getting background values")
        r_background = np.median(np.array(r_channel_background)[:, method_idx])
        g_background = np.median(np.array(g_channel_background)[:, method_idx])
        b_background = np.median(np.array(b_channel_background)[:, method_idx])
        self.logger.debug("Background subtraction: Region {}\t{}\t{}\t{}".format(
            background_region_id, r_background, g_background, b_background))

        # self.logger.debug("Getting sigma values")
        # r_sigma = np.median(np.array(r_channel_background)[:, 2])
        # g_sigma = np.median(np.array(g_channel_background)[:, 2])
        # b_sigma = np.median(np.array(b_channel_background)[:, 2])

        self.logger.debug("Getting RGB data")
        r_masked_data = np.ma.array(stamp, mask=~r_mask)
        g_masked_data = np.ma.array(stamp, mask=~g_mask)
        b_masked_data = np.ma.array(stamp, mask=~b_mask)

        # self.logger.debug("Clipping RGB values with 5-sigma: {} {} {}".format(r_sigma, g_sigma, b_sigma))
        # np.ma.clip(r_masked_data, r_background - 5 * r_sigma, r_background + 5 * r_sigma, r_masked_data)
        # np.ma.clip(g_masked_data, g_background - 5 * g_sigma, g_background + 5 * g_sigma, g_masked_data)
        # np.ma.clip(b_masked_data, b_background - 5 * b_sigma, b_background + 5 * b_sigma, b_masked_data)

        self.logger.debug("Subtracting backgrounds")
        r_masked_data -= r_background
        g_masked_data -= g_background
        b_masked_data -= b_background

        self.logger.debug("Combining channels")
        subtracted_data = r_masked_data.filled(0) + g_masked_data.filled(0) + b_masked_data.filled(0)

        return subtracted_data

    def subtract_background(self, frames=None):
        """Get background estimates for all frames for each color channel

        The first step is to figure out a box size for the background calculations.
        This should be larger enough to encompass background variations while also
        being an even multiple of the image dimensions. We also want them to be
        multiples of a superpixel (2x2 regular pixel) in each dimension.
        The camera for `PAN001` has image dimensions of 5208 x 3476, so
        in order to get an even multiple in both dimensions we remove 60 pixels
        from the width of the image, leaving us with dimensions of 5148 x 3476,
        allowing us to use a box size of 468 x 316, which will create 11
        boxes in each direction.

        We use a 3 sigma median background clipped estimator.
        The built-in camera bias (1024) has already been removed from the data.

        Args:
            frames (list, optional): List of frames to get estimates for, defaults
                to all frames

        """
        self.logger.debug("Getting background estimates")
        if frames is None:
            frames = range(len(self.files))

        sigma_clip = SigmaClip(sigma=3., iters=10)
        bkg_estimator = MedianBackground()

        for frame_index in frames:

            self.logger.debug("Frame: {}".format(frame_index))

            # Get the bias subtracted data for the frame
            data = self.data_cube[frame_index]

            if self.rgb_masks is None:
                # Create RGB masks
                self.logger.debug("Making RGB masks")
                self.rgb_masks = utils.make_masks(data)

            for color, mask in zip(['R', 'G', 'B'], self.rgb_masks):
                bkg = Background2D(data, (self.background_box_h, self.background_box_w), filter_size=(3, 3),
                                   sigma_clip=sigma_clip, bkg_estimator=bkg_estimator, mask=~mask)

                self.logger.debug("\t{} Background\t Value: {:.02f}\t RMS: {:.02f}".format(
                    color, bkg.background_median, bkg.background_rms_median))

                background_masked_data = np.ma.array(bkg.background, mask=~mask)

                self.data_cube[frame_index] -= background_masked_data.filled(0)

    def get_source_slice(self, source_index, force_new=False, cache=True, *args, **kwargs):
        """ Create a stamp (stamp) of the data

        This uses the start and end points from the source drift to figure out
        an appropriate size to stamp. Data is bias and background subtracted.
        """
        try:
            if force_new:
                del self._stamps_cache[source_index]
            stamp = self._stamps_cache[source_index]
        except KeyError:
            start_pos, mid_pos, end_pos = self._get_stamp_points(source_index)
            mid_pos = self._adjust_stamp_midpoint(mid_pos)

            # Get the width and height of data region
            width, height = (start_pos - end_pos)

            cutout = Cutout2D(
                fits.getdata(self.files[0]),
                (mid_pos[0], mid_pos[1]),
                (self._pad_super_pixel(height) + 8, self._pad_super_pixel(width) + 4)
            )

            xs, ys = cutout.bbox_original

            # Shared across all stamps
            self.stamp_size = cutout.data.shape

            # Don't carry around the data
            cutout.data = []

            stamp = Stamp(
                row_slice=slice(xs[0], xs[1] + 1),
                col_slice=slice(ys[0], ys[1] + 1),
                mid_point=mid_pos,
                cutout=cutout,
            )

            if cache:
                self._stamps_cache[source_index] = stamp

        return stamp

    def get_source_fluxes(self, source_index):
        """ Get fluxes for given source

        Args:
            source_index (int): Index of the source from `point_sources`

        Returns:
            numpy.array: 1-D array of fluxes
        """
        fluxes = []

        stamps = self.get_source_stamps(source_index)

        # Get aperture photometry
        for i in self.pixel_locations:
            x = int(self.pixel_locations[i, source_index, 0] - stamps[i].origin_original[0]) - 0.5
            y = int(self.pixel_locations[i, source_index, 1] - stamps[i].origin_original[1]) - 0.5

            aperture = RectangularAperture((x, y), w=6, h=6, theta=0)

            phot_table = aperture_photometry(stamps[i].data, aperture)

            flux = phot_table['aperture_sum'][0]

            fluxes.append(flux)

        fluxes = np.array(fluxes)

        return fluxes

    def get_frame_stamp(self, source_index, frame_index,
                        subtract_background=True, get_subtracted=True, reshape=False, *args, **kwargs):
        """ Get individual stamp for given source and frame

        Note:
            Data is bias and background subtracted

        Args:
            source_index (int): Index of the source from `point_sources`
            frame_index (int): Index of the frame from `files`
            *args (TYPE): Description
            **kwargs (TYPE): Description

        Returns:
            numpy.array: Array of data
        """

        try:
            stamp = self._hdf5_subtracted[
                'subtracted/{}'.format(source_index)][frame_index]

            if reshape:
                num_rows = self._hdf5_subtracted.attrs['stamp_rows']
                num_cols = self._hdf5_subtracted.attrs['stamp_cols']
                stamp = stamp.reshape(num_rows, num_cols).astype(int)
        except KeyError:
            stamp_slice = self.get_source_slice(source_index, *args, **kwargs)
            stamp = self.data_cube[frame_index, stamp_slice.row_slice, stamp_slice.col_slice]

            if subtract_background:

                # NEED TO FIGURE OUT THE RGB MASKS FOR STAMP

                stamp = self.subtract_background(stamp, frame_index, mid_point=stamp_slice.mid_point).astype(int)

        return stamp

    def get_frame_aperture(self, source_index, frame_index, width=6, height=6, *args, **kwargs):
        """Aperture for given frame from source

        Note:
            `width` and `height` should be in multiples of 2 to get a super-pixel

        Args:
            source_index (int): Index of the source from `point_sources`
            frame_index (int): Index of the frame from `files`
            width (int, optional): Width of the aperture, defaults to 3x2=6
            height (int, optional): Height of the aperture, defaults to 3x2=6
            *args (TYPE): Description
            **kwargs (TYPE): Description

        Returns:
            photutils.RectangularAperture: Aperture surrounding the frame
        """
        stamp_slice = self.get_source_slice(source_index, *args, **kwargs)

        x = int(self.pixel_locations[frame_index, source_index, 0] - stamp_slice.cutout.origin_original[0]) - 0.5
        y = int(self.pixel_locations[frame_index, source_index, 1] - stamp_slice.cutout.origin_original[1]) - 0.5

        aperture = RectangularAperture((x, y), w=width, h=height, theta=0)

        return aperture

    def plot_stamp(self, source_index, frame_index, show_data=False, *args, **kwargs):

        norm = ImageNormalize(stretch=SqrtStretch())

        stamp_slice = self.get_source_slice(source_index, *args, **kwargs)
        stamp = self.get_frame_stamp(source_index, frame_index, reshape=True, *args, **kwargs)

        fig = plt.figure(1)
        fig.set_size_inches(13, 15)
        gs = gridspec.GridSpec(2, 2, width_ratios=[1, 1])
        ax1 = plt.subplot(gs[:, 0])
        ax2 = plt.subplot(gs[0, 1])
        ax3 = plt.subplot(gs[1, 1])
        fig.add_subplot(ax1)
        fig.add_subplot(ax2)
        fig.add_subplot(ax3)

        aperture = self.get_frame_aperture(source_index, frame_index, return_aperture=True)

        aperture_mask = aperture.to_mask(method='center')[0]
        aperture_data = aperture_mask.cutout(stamp)

        phot_table = aperture_photometry(stamp, aperture, method='center')

        if show_data:
            print(np.flipud(aperture_data))  # Flip the data to match plot

        cax1 = ax1.imshow(stamp, cmap='cubehelix_r', norm=norm)
        plt.colorbar(cax1, ax=ax1)

        aperture.plot(color='b', ls='--', lw=2, ax=ax1)

        # Bayer pattern
        for i, val in np.ndenumerate(stamp):
            x, y = stamp_slice.cutout.to_original_position((i[1], i[0]))
            ax1.text(x=i[1], y=i[0], ha='center', va='center',
                     s=utils.pixel_color(x, y, zero_based=True), fontsize=10, alpha=0.25)

        # major ticks every 2, minor ticks every 1
        x_major_ticks = np.arange(-0.5, stamp_slice.cutout.bbox_cutout[1][1], 2)
        x_minor_ticks = np.arange(-0.5, stamp_slice.cutout.bbox_cutout[1][1], 1)

        y_major_ticks = np.arange(-0.5, stamp_slice.cutout.bbox_cutout[0][1], 2)
        y_minor_ticks = np.arange(-0.5, stamp_slice.cutout.bbox_cutout[0][1], 1)

        ax1.set_xticks(x_major_ticks)
        ax1.set_xticks(x_minor_ticks, minor=True)
        ax1.set_yticks(y_major_ticks)
        ax1.set_yticks(y_minor_ticks, minor=True)

        ax1.grid(which='major', color='r', linestyle='-', alpha=0.25)
        ax1.grid(which='minor', color='r', linestyle='-', alpha=0.1)

        ax1.set_xticklabels([])
        ax1.set_yticklabels([])
        ax1.set_title("Full Stamp", fontsize=16)

        # RGB values plot

        # Show numbers
        for i, val in np.ndenumerate(aperture_data):
            #     print(i[0] / 10, i[1] / 10, val)
            x_loc = (i[1] / 10) + 0.05
            y_loc = (i[0] / 10) + 0.05

            ax2.text(x=x_loc, y=y_loc,
                     ha='center', va='center', s=val, fontsize=14, alpha=0.75, transform=ax2.transAxes)

        ax2.set_xticks(x_major_ticks)
        ax2.set_xticks(x_minor_ticks, minor=True)
        ax2.set_yticks(y_major_ticks)
        ax2.set_yticks(y_minor_ticks, minor=True)

        ax2.grid(which='major', color='r', linestyle='-', alpha=0.25)
        ax2.grid(which='minor', color='r', linestyle='-', alpha=0.1)

        ax2.add_patch(patches.Rectangle(
            (1.5, 1.5),
            6, 6,
            fill=False,
            lw=2,
            ls='dashed',
            edgecolor='blue',
        ))
        ax2.add_patch(patches.Rectangle(
            (0, 0),
            9, 9,
            fill=False,
            lw=1,
            ls='solid',
            edgecolor='black',
        ))

        r_a_mask, g_a_mask, b_a_mask = utils.make_masks(aperture_data)

        ax2.set_xlim(-0.5, 9.5)
        ax2.set_ylim(-0.5, 9.5)
        ax2.set_xticklabels([])
        ax2.set_yticklabels([])
        ax2.imshow(np.ma.array(np.ones((10, 10)), mask=~r_a_mask), cmap='Reds', vmin=0, vmax=4., )
        ax2.imshow(np.ma.array(np.ones((10, 10)), mask=~g_a_mask), cmap='Greens', vmin=0, vmax=4., )
        ax2.imshow(np.ma.array(np.ones((10, 10)), mask=~b_a_mask), cmap='Blues', vmin=0, vmax=4., )
        ax2.set_title("Values", fontsize=16)

        # Contour Plot of aperture

        ax3.contourf(aperture_data, cmap='cubehelix_r', vmin=stamp.min(), vmax=stamp.max())
        ax3.add_patch(patches.Rectangle(
            (1.5, 1.5),
            6, 6,
            fill=False,
            lw=2,
            ls='dashed',
            edgecolor='blue',
        ))
        ax3.add_patch(patches.Rectangle(
            (0, 0),
            9, 9,
            fill=False,
            lw=1,
            ls='solid',
            edgecolor='black',
        ))
        ax3.set_xlim(-0.5, 9.5)
        ax3.set_ylim(-0.5, 9.5)
        ax3.set_xticklabels([])
        ax3.set_yticklabels([])
        ax3.grid(False)
        ax3.set_facecolor('white')
        ax3.set_title("Contour", fontsize=16)

        fig.suptitle("Source {} Frame {} Aperture Flux: {}".format(source_index,
                                                                   frame_index, int(phot_table['aperture_sum'][0])),
                     fontsize=20)

        fig.tight_layout(rect=[0., 0., 1., 0.95])
        return fig

    def create_stamps(self, remove_cube=False, *args, **kwargs):
        """Create subtracted stamps for entire data cube

        Creates a slice through the cube corresponding to a stamp and stores the
        subtracted data in the hdf5 table with key `subtracted/<index>`, where
        `<index>` is the source index from `point_sources`

        Args:
            remove_cube (bool, optional): Remove the full cube from the hdf5 file after
                processing, defaults to False
            *args (TYPE): Description
            **kwargs (dict): `ipython_widget=True` can be passed to display progress
                within a notebook

        """

        self.logger.debug("Starting stamp creation")
        for source_index in ProgressBar(self.point_sources.index,
                                        ipython_widget=kwargs.get('ipython_widget', False)):

            subtracted_group_name = 'subtracted/{}'.format(source_index)
            if subtracted_group_name not in self._hdf5_subtracted:

                try:
                    ss = self.get_source_slice(source_index)
                    stamps = np.array(self.data_cube[:, ss.row_slice, ss.col_slice])

                    # Store
                    self._hdf5_subtracted.create_dataset(subtracted_group_name, data=stamps)

                except Exception as e:
                    self.logger.warning("Problem creating subtracted stamp for {}: {}".format(source_index, e))

        # Store stamp size
        try:
            self._hdf5_subtracted.attrs['stamp_rows'] = ss.cutout.shape[0]
            self._hdf5_subtracted.attrs['stamp_cols'] = ss.cutout.shape[1]
        except UnboundLocalError:
            pass

    def get_variance_for_target(self, target_index, force_new=False, show_progress=True, *args, **kwargs):
        """ Get all variances for given target

        Args:
            stamps(np.array): Collection of stamps with axes: frame, PIC, pixels
            i(int): Index of target PIC
        """
        num_sources = len(self.point_sources)

        try:
            vgrid_dset = self._hdf5_subtracted['vgrid']
        except KeyError:
            vgrid_dset = self._hdf5_subtracted.create_dataset('vgrid', (num_sources, num_sources))

        stamp0 = np.array(self._hdf5_subtracted['subtracted/{}'.format(target_index)])

        # Normalize
        self.logger.debug("Normalizing target")
        stamp0 = stamp0 / stamp0.sum()

        if show_progress:
            iterator = ProgressBar(range(num_sources), ipython_widget=kwargs.get('ipython_widget', False))
        else:
            iterator = range(num_sources)

        for source_index in iterator:
            # Only compute if zero (which will re-compute target but that's fine)
            if vgrid_dset[target_index, source_index] == 0. and vgrid_dset[source_index, target_index] == 0.:
                stamp1 = np.array(self._hdf5_subtracted['subtracted/{}'.format(source_index)])

                # Normalize
                stamp1 = stamp1 / stamp1.sum()

                # Store in the grid
                try:
                    vgrid_dset[target_index, source_index] = ((stamp0 - stamp1) ** 2).sum()
                except ValueError:
                    self.logger.debug("Skipping invalid stamp for source {}".format(source_index))

    def lookup_point_sources(self, image_num=0, sextractor_params=None, force_new=False):
        """ Extract point sources from image

        Args:
            image_num (int, optional): Frame number of observation from which to
                extract images
            sextractor_params (dict, optional): Parameters for sextractor,
                defaults to settings contained in the `panoptes.sex` file
            force_new (bool, optional): Force a new catalog to be created,
                defaults to False

        Raises:
            error.InvalidSystemCommand: Description
        """
        # Write the sextractor catalog to a file
        source_file = '{}/point_sources_{:02d}.cat'.format(self.image_dir, image_num)
        self.logger.debug("Point source catalog: {}".format(source_file))

        if not os.path.exists(source_file) or force_new:
            self.logger.debug("No catalog found, building from sextractor")
            # Build catalog of point sources
            sextractor = shutil.which('sextractor')
            if sextractor is None:
                sextractor = shutil.which('sex')
                if sextractor is None:
                    raise Exception('sextractor not found')

            if sextractor_params is None:
                sextractor_params = [
                    '-c', '{}/PIAA/resources/conf_files/sextractor/panoptes.sex'.format(os.getenv('PANDIR')),
                    '-CATALOG_NAME', source_file,
                ]

            self.logger.debug("Running sextractor...")
            cmd = [sextractor, *sextractor_params, self.files[image_num]]
            self.logger.debug(cmd)
            subprocess.run(cmd)

        # Read catalog
        point_sources = Table.read(source_file, format='ascii.sextractor')

        # Remove the point sources that sextractor has flagged
        if 'FLAGS' in point_sources.keys():
            point_sources = point_sources[point_sources['FLAGS'] == 0]
            point_sources.remove_columns(['FLAGS'])

        # Rename columns
        point_sources.rename_column('X_IMAGE', 'X')
        point_sources.rename_column('Y_IMAGE', 'Y')

        # Filter point sources near edge
        # w, h = data[0].shape
        w, h = (3476, 5208)

        stamp_size = 60

        top = point_sources['Y'] > stamp_size
        bottom = point_sources['Y'] < w - stamp_size
        left = point_sources['X'] > stamp_size
        right = point_sources['X'] < h - stamp_size

        self._point_sources = point_sources[top & bottom & right & left].to_pandas()

        return self._point_sources

    # def build_all_psc(self):
    #     # Make a data cube for the entire observation
    #     cube = list()

    #     for i, f in enumerate(self.files):
    #         with fits.open(f) as hdu:
    #             d0 = hdu[0].data

    #             stamps = [utils.make_postage_stamp(d0, ps['X'], ps['Y'], padding=self.stamp_padding).flatten()
    #                       for ps in self.point_sources]

    #             cube.append(stamps)

    #             hdu.close()

    #     self.psc_collection = np.array(cube)

    # def get_variance(self, frame, i, j):
    #     """ Compare one stamp to another and get variance

    #     Args:
    #         stamps(np.array): Collection of stamps with axes: frame, PIC, pixels
    #         frame(int): The frame number we want to compare
    #         i(int): Index of target PIC
    #         j(int): Index of PIC we want to compare target to
    #     """

    #     normal_target = self.psc_collection[frame, i] / self.psc_collection[frame, i].sum()
    #     normal_compare = self.psc_collection[frame, j] / self.psc_collection[frame, j].sum()

    #     normal_diff = (normal_target - normal_compare)**2

    #     diff_sum = normal_diff.sum()

    #     return diff_sum

    # def get_all_variance(self, i):
    #     """ Get all variances for given target

    #     Args:
    #         stamps(np.array): Collection of stamps with axes: frame, PIC, pixels
    #         i(int): Index of target PIC
    #     """

    #     v = np.zeros((self.num_stars))

    #     for m in range(self.num_frames):
    #         for j in range(self.num_stars):
    #             v[j] += self.get_variance(m, i, j)

    #     s = pd.Series(v)
    #     return s

    def _get_stamp_points(self, idx):
        # Print beginning, middle, and end positions
        start_pos = self.pixel_locations.iloc[0, idx]
        mid_pos = self.pixel_locations.iloc[int(len(self.files) / 2), idx]
        end_pos = self.pixel_locations.iloc[-1, idx]

        return start_pos, mid_pos, end_pos

    def _adjust_stamp_midpoint(self, mid_pos):
        """ The midpoint pixel should always end up as Blue to accommodate slicing """
        color = utils.pixel_color(mid_pos[0], mid_pos[1])

        x = mid_pos[0]
        y = mid_pos[1]

        if color == 'G2':
            x -= 1
        elif color == 'G1':
            y -= 1
        elif color == 'B':
            x -= 1
        elif color == 'R':
            x += 1
            y += 1

        y += 4
        x -= 2

        return (x, y)

    def _pad_super_pixel(self, num):
        """ Get the nearest 10 block """
        return int(np.ceil(np.abs(num) / 8)) * 8

    def _load_images(self, remove_pointing=True):
        seq_files = glob("{}/*.fits".format(self.image_dir))
        seq_files.sort()

        self.files = seq_files
