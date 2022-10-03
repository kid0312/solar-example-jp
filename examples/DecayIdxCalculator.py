import datetime
import glob
import json
import copy
import math
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.patheffects import withStroke
from matplotlib.ticker import MultipleLocator
import numpy as np
from scipy import interpolate, optimize

import astropy
import astropy.units as u
import astropy.constants as const
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS

import sunpy
import sunpy.map
from sunpy.map.sources.sdo import HMIMap #, HMISynopticMap
from sunpy.physics.differential_rotation import solar_rotate_coordinate
from sunpy.net import Fido, attrs as a
from sunpy.coordinates import HeliographicCarrington, Helioprojective, RotatedSunFrame, transform_with_sun_center, propagate_with_solar_surface
from sunpy.coordinates.sun import carrington_rotation_number

import pfsspy
import pfsspy.utils
import pfsspy.tracing as tracing
from reproject import reproject_interp

# set your registered email to JSOC_EMAIL
from config import *

"""
should be fix:

- the observed times of downloaded hmi and aia tend to have gap,
so it would be better if one map is reprojected to be another map.
- about the value of header.cdelt2
- 
"""

decay_list = []

class DecayIdxCalculator:
    def __init__(self, nr, rss):
        self.jsoc_email = JSOC_EMAIL
        self.nr = nr
        self.rss = rss
        self.effects = [withStroke(linewidth=2, foreground="white")]
        self.log = {'nr': nr, 'rss': rss}

    def _fido_get_aia(self, wl_aia, t_aia):
        self.t_aia = astropy.time.Time(t_aia, scale='utc')
        _q_aia = Fido.search(
            a.Time(self.t_aia, self.t_aia + 5 * u.min),
            a.Wavelength(wl_aia * u.angstrom),
            a.Instrument.aia,
            # a.jsoc.Notify(JSOC_EMAIL),
        )
        _d_aia = Fido.fetch(_q_aia[0][0])
        if self.showfilename:
            print(_q_aia[0][0])
        return sunpy.map.Map(_d_aia)

    def _fido_get_hmi(self, t_hmi):
        _t_hmi = astropy.time.Time(t_hmi, scale='utc')
        _q_hmi = Fido.search(
            a.Time(_t_hmi, _t_hmi + 5 * u.min),
            a.Instrument.hmi,
            a.Physobs.los_magnetic_field,
            # a.jsoc.Notify(JSOC_EMAIL),
        )
        _d_hmi = Fido.fetch(_q_hmi[0][0])
        if self.showfilename:
            print(_q_hmi[0][0])
        return sunpy.map.Map(_d_hmi)

    def _fido_get_synoptic(self):
        # _t_syn = astropy.time.Time(_t_syn, scale='utc')
        _car_num = carrington_rotation_number(self.t_aia)
        _q_syn = Fido.search(
            a.Time(self.t_aia, self.t_aia),
            a.jsoc.Series('hmi.synoptic_mr_polfil_720s'),
            a.jsoc.PrimeKey('CAR_ROT', int(_car_num)),
            a.jsoc.Notify(JSOC_EMAIL),
        )
        _d_syn = Fido.fetch(_q_syn[0][0])
        if self.showfilename:
            print(_d_syn[0][0])
        return sunpy.map.Map(_d_syn)

    def set_fido_file(self, wl_aia, t_aia, t_hmi, downsample=True, showfilename=False):
        self.showfilename = showfilename
        self.map_downsample = downsample
        self.aiamap = self._fido_get_aia(wl_aia, t_aia)
        self.hmimap = self._fido_get_hmi(t_hmi)
        self.synmap = self._fido_get_synoptic()
        self._reproject_maps()

    def set_local_file(self, amap, hmap, smap, downsample=True):
        self.map_downsample = downsample
        self.aiamap = amap
        self.hmimap = hmap
        self.synmap = smap
        self._reproject_maps()

    def set_local_path(self, apath, hpath, spath, downsample=True):
        self.map_downsample = downsample
        self.log['aia_path'] = apath
        self.log['hmi_path'] = hpath
        self.log['syn_path'] = spath
        self.aiamap = sunpy.map.Map(apath)
        self.hmimap = sunpy.map.Map(hpath)
        self.synmap = sunpy.map.Map(spath)
        self._reproject_maps()

    def _reproject_maps(self): # TODO overlay hmi batch to synoptic
        if self.map_downsample:
            self.new_aiamap = self.aiamap.resample([2048, 2048]*u.pix)
        else:
            self.new_aiamap = self.aiamap
        self.new_hmimap = self._hmi_to_aia()
        # self.new_synmap = self._overlay_hmi_to_syn()

    # TODO
    # the development of reprojection is almost done, but it takes ~1 minutes when you
    # use full resolution AIA/HMI map, so it would be better for the reprojection
    # is applied only for cropped map ?
    def _hmi_to_aia(self):
        return self.hmimap
        # if self.map_downsample:
        #     _hmap = self.hmimap.resample([2048, 2048]*u.pix)
        # else:
        #     _hmap = self.hmimap
        # out_frame = Helioprojective(observer='earth', obstime=self.new_aiamap.date)
        # out_center = SkyCoord(0*u.arcsec, 0*u.arcsec, frame=out_frame)
        # header = sunpy.map.make_fitswcs_header(self.new_aiamap.data.shape,
        #                                     out_center,
        #                                     scale=u.Quantity(_hmap.scale))
        # out_wcs = WCS(header)
        # with propagate_with_solar_surface():
        #     warped_hmap = _hmap.reproject_to(out_wcs)
        # newmap = HMIMap(warped_hmap.data, warped_hmap.meta)
        # newmap.meta['bunit'] = 'Gauss' # necessary for hmi contour
        # return newmap

    # TODO
    def _overlay_hmi_to_syn(self):
        self.resampled_synmap = self.synmap.resample([720, 360] * u.pix)
        # return self.synmap.resample([720, 360] * u.pix) # DEBUG
        # set mask and get the small area in hmi
        all_hpc = sunpy.map.all_coordinates_from_map(self.hmimap)
        segment_mask_x = np.logical_or(all_hpc.Tx >= self.trc_h.Tx, all_hpc.Tx <= self.blc_h.Tx)
        segment_mask_y = np.logical_or(all_hpc.Ty >= self.trc_h.Ty, all_hpc.Ty <= self.blc_h.Ty)
        segment_mask = (segment_mask_x | segment_mask_y | np.isnan(all_hpc.Tx) | np.isnan(all_hpc.Ty))

        newdata = copy.deepcopy(self.hmimap.data)
        newdata[np.where(segment_mask==True)] = np.nan
        newmap = sunpy.map.Map(newdata, self.hmimap.meta)

        # reproject the cropped small hmi to synoptic
        # if you consider differential rotation in the same domain (hmi > hmi), 
        # you just need ``propagete_with_solar_surface'', but when the domain will be changed,
        # you seem to need ``transform_with_sun_center'' ... right?
        out_frame = HeliographicCarrington(observer='earth', obstime=self.resampled_synmap.date)
        rot_frame = RotatedSunFrame(base=out_frame, rotated_time=self.hmimap.date)
        out_shape = self.resampled_synmap.data.shape
        out_wcs = self.resampled_synmap.wcs
        out_wcs.coordinate_frame = rot_frame
        with transform_with_sun_center():
            reprojected_data, footprint = reproject_interp(newmap, out_wcs, out_shape)

        repro_and_seg = copy.deepcopy(self.resampled_synmap.data)
        repro_and_seg = np.where(np.isnan(reprojected_data), repro_and_seg, reprojected_data)
        repro_and_seg = np.where(np.isnan(repro_and_seg), 0, repro_and_seg) # replace np.nan in the raw synoptic

        reprojected_map = sunpy.map.Map(repro_and_seg, self.resampled_synmap.meta)
        return reprojected_map

    def _onclick_preplot(self, event):
        # get clicked pixel coordinates
        _x = event.xdata
        _y = event.ydata
        print('click: xdata=%f, ydata=%f' % (_x, _y))
        # transform the pixel to world (solar surface) coordinates
        self.center_coord = self.new_aiamap.pixel_to_world(_x*u.pix, _y*u.pix)

    # button click and put marker function
    def _onclick(self, event):
        # get clicked pixel coordinates
        _x = event.xdata
        _y = event.ydata
        print('click: xdata=%f, ydata=%f' % (_x, _y))
        
        # transform the pixel to world (solar surface) coordinates
        coord = self.cropped_aiamap.pixel_to_world(_x*u.pix, _y*u.pix)
        self.log['coords'].append([coord.Tx.value, coord.Ty.value])
        
        # calibrate the difference of observed time
        # I should solve some warnings about the difference between "solar time" "Earth time"?
        coord_heligra = coord.transform_to(sunpy.coordinates.HeliographicCarrington)
        coord_syn = solar_rotate_coordinate(coord_heligra, time=self.new_synmap.date)
        c_spix = self.new_synmap.world_to_pixel(coord_syn)

        # with open(f"coord_{dt_str}.txt", mode="a+") as f:
        #     f.writelines(f"{c_spix[0].value},{c_spix[1].value}\n")

        self.ax_click.plot_coord(coord, marker="+", linewidth=10, markersize=12, path_effects=self.effects)
        self.ax_syn.plot_coord(coord_syn, color="white", marker="+", linewidth=5, markersize=10)

        di_click_point = self.click_point_decay(c_spix.x.value, c_spix.y.value)
        # di_one, h_one = self.interp_decay(di, , height, h_limit)
        for i in range(di_click_point.shape[0]):
            for j in range(di_click_point.shape[1]):
                self.decay_index_list.append(di_click_point[i,j])
        self.ax_eachdi.plot(self.h_interp, self.interp_decay(np.average(di_click_point, axis=(0,1)))[0])

        di_ave = np.average(np.array(self.decay_index_list), axis=0)
        di_std = np.std(np.array(self.decay_index_list), axis=0)
        self.ax_avedi.cla()
        di_interp, key_height = self.interp_decay(di_ave)
        self.ax_avedi.vlines(x=key_height, ymin=0, ymax=1.5, color="red")
        self.ax_avedi.text(key_height, 2.5, f"h_crit = {key_height:.1f} Mm", horizontalalignment="center", color="red")
        self.ax_avedi.plot(self.h_interp, di_interp, color="black")
        self.ax_avedi.errorbar(self.h_limited, di_ave, yerr=di_std, fmt="none", ecolor="black", elinewidth=1)
        self.ax_avedi.set_ylim([0, 4.5])
        self.ax_avedi.set_title("Averaged Decay Index")
        self.ax_avedi.set_xlabel("height to solar surface [Mm]")
        self.ax_avedi.set_ylabel("decay index")
        self.ax_avedi.grid()
        plt.draw()

    def cal_decay_ss(self, h_threshold = 200, key_di = 1.5):
        self.new_synmap = self._overlay_hmi_to_syn()
        if key_di is not None:
            self.log['key_di'] = key_di
            self.key_di = key_di
        # 'h_threshold' is the maximal height to interpolate (unit: Mm)
        if h_threshold is not None:
            self.h_threshold = h_threshold
        self.decay_index_list = []
        pfss_in = pfsspy.Input(self.new_synmap, self.nr, self.rss)
        self.pfss_out = pfsspy.pfss(pfss_in)
        
        b_theta = self.pfss_out.bg[:,:,:,1]
        b_phi = self.pfss_out.bg[:,:,:,0]
        # b_r = pfss_out.bg[:,:,:,2]
        bh = np.sqrt(b_theta*b_theta + b_phi*b_phi)
        h = (np.exp(self.pfss_out.grid.rg) - 1)[1:]

        dln_bh = np.diff(np.log(bh[:,:,1:]), axis=-1)
        dln_h = np.diff(np.log(h))
        di = -dln_bh/dln_h
        # transpose axis same as synoptic map
        # di = di.transpose(1,0,2)
        # which is the valid??
        # assume the index at solar surface to be same as the index one step above
        # di = np.pad(di, [(0,0),(0,0),(1,0)], "edge")
        # assume the index at solar surface as zero, default constant value is zero
        di = np.pad(di, [(0,0),(0,0),(1,0)], "constant")
        
        self.decay_index = di
        # self.calc_height = h
        self.height_Mm = (np.exp(self.pfss_out.grid.rg) - 1) * const.R_sun.to("Mm").value

        self.h_limited = self.height_Mm[np.where(self.height_Mm<=self.h_threshold)[0]]
        self.h_interp = np.linspace(0, math.floor(self.h_limited[-1]), 1000)

        fig = plt.figure(figsize=(16, 8))
        gs = matplotlib.gridspec.GridSpec(2,4)
        self.log['coords'] = []
        cid = fig.canvas.mpl_connect('button_press_event', self._onclick)

        # draw synoptic 
        # self.ax_syn = plt.subplot(gs[0,:2], projection=self.new_synmap)
        # self.new_synmap.plot(axes=self.ax_syn)
        # Plot the source surface map
        ss_br = self.pfss_out.source_surface_br
        self.ax_syn = plt.subplot(gs[0,:2], projection=ss_br)
        ss_br.plot(axes=self.ax_syn)
        # Plot the polarity inversion line
        self.ax_syn.plot_coord(self.pfss_out.source_surface_pils[0])


        # plot each decay index 
        self.ax_eachdi = plt.subplot(gs[1, 0])
        self.ax_eachdi.set_title("Decay Index at each point")
        self.ax_eachdi.set_ylim([0, 4.5])
        self.ax_eachdi.set_xlabel("height to solar surface [Mm]")
        self.ax_eachdi.set_ylabel("decay index")
        self.ax_eachdi.grid()

        # plot averageed decay index
        self.ax_avedi = plt.subplot(gs[1, 1])

        # draw AIA and HMI contour
        self.ax_click = plt.subplot(gs[:2,2:4], projection=self.cropped_aiamap)
        self.cropped_aiamap.plot(axes=self.ax_click, clip_interval=(1, 99.99)*u.percent)
        grid = self.cropped_aiamap.draw_grid()

        # levels = [-1000, -100, 100, 1000] * u.Gauss
        # levels = [-500, -100, 100, 500] * u.Gauss
        levels = [-50, 50] * u.Gauss
        cset = self.cropped_hmimap.draw_contours(levels, axes=self.ax_click, cmap="bwr", alpha=0.7)

        plt.show()

    def cal_decay(self, h_threshold = 200, key_di = 1.5):
        self.new_synmap = self._overlay_hmi_to_syn()
        if key_di is not None:
            self.log['key_di'] = key_di
            self.key_di = key_di
        # 'h_threshold' is the maximal height to interpolate (unit: Mm)
        if h_threshold is not None:
            self.h_threshold = h_threshold
        self.decay_index_list = []
        pfss_in = pfsspy.Input(self.new_synmap, self.nr, self.rss)
        self.pfss_out = pfsspy.pfss(pfss_in)
        
        b_theta = self.pfss_out.bg[:,:,:,1]
        b_phi = self.pfss_out.bg[:,:,:,0]
        # b_r = pfss_out.bg[:,:,:,2]
        bh = np.sqrt(b_theta*b_theta + b_phi*b_phi)
        h = (np.exp(self.pfss_out.grid.rg) - 1)[1:]

        dln_bh = np.diff(np.log(bh[:,:,1:]), axis=-1)
        dln_h = np.diff(np.log(h))
        di = -dln_bh/dln_h
        # transpose axis same as synoptic map
        # di = di.transpose(1,0,2)
        # which is the valid??
        # assume the index at solar surface to be same as the index one step above
        # di = np.pad(di, [(0,0),(0,0),(1,0)], "edge")
        # assume the index at solar surface as zero, default constant value is zero
        di = np.pad(di, [(0,0),(0,0),(1,0)], "constant")
        
        self.decay_index = di
        # self.calc_height = h
        self.height_Mm = (np.exp(self.pfss_out.grid.rg) - 1) * const.R_sun.to("Mm").value

        self.h_limited = self.height_Mm[np.where(self.height_Mm<=self.h_threshold)[0]]
        self.h_interp = np.linspace(0, math.floor(self.h_limited[-1]), 1000)

        fig = plt.figure(figsize=(16, 8))
        gs = matplotlib.gridspec.GridSpec(2,4)
        self.log['coords'] = []
        cid = fig.canvas.mpl_connect('button_press_event', self._onclick)

        # draw synoptic 
        self.ax_syn = plt.subplot(gs[0,:2], projection=self.new_synmap)
        self.new_synmap.plot(axes=self.ax_syn)

        # plot each decay index 
        self.ax_eachdi = plt.subplot(gs[1, 0])
        self.ax_eachdi.set_title("Decay Index at each point")
        self.ax_eachdi.set_ylim([0, 4.5])
        self.ax_eachdi.set_xlabel("height to solar surface [Mm]")
        self.ax_eachdi.set_ylabel("decay index")
        self.ax_eachdi.grid()

        # plot averageed decay index
        self.ax_avedi = plt.subplot(gs[1, 1])

        # draw AIA and HMI contour
        self.ax_click = plt.subplot(gs[:2,2:4], projection=self.cropped_aiamap)
        self.cropped_aiamap.plot(axes=self.ax_click, clip_interval=(1, 99.99)*u.percent)
        grid = self.cropped_aiamap.draw_grid()

        # levels = [-1000, -100, 100, 1000] * u.Gauss
        # levels = [-500, -100, 100, 500] * u.Gauss
        levels = [-50, 50] * u.Gauss
        cset = self.cropped_hmimap.draw_contours(levels, axes=self.ax_click, cmap="bwr", alpha=0.7)

        plt.show()

    def plotlog(self, imgpath):
        plt.rcParams["font.family"] = "Arial"
        plt.rcParams["font.size"] = 20   
        # fig_ss = plt.figure(figsize=(8,8))
        fig_di_each = plt.figure(figsize=(8,6))
        ax_di_each = fig_di_each.add_subplot(111)
        ax_di_each.set_title("Decay Index at each point")
        ax_di_each.set_ylim([0, 4.5])
        ax_di_each.set_xlabel("height to solar surface [Mm]")
        ax_di_each.set_ylabel("decay index")
        ax_di_each.xaxis.set_minor_locator(MultipleLocator(10))
        ax_di_each.xaxis.set_major_locator(MultipleLocator(50))
        ax_di_each.yaxis.set_major_locator(MultipleLocator(1))
        ax_di_each.yaxis.set_minor_locator(MultipleLocator(0.5))
        ax_di_each.grid()

        fig_di_total = plt.figure(figsize=(8,6))
        ax_di_total = fig_di_total.add_subplot(111)
        ax_di_total.set_ylim([0, 4.5])
        ax_di_total.set_title("Averaged Decay Index")
        ax_di_total.set_xlabel("height to solar surface [Mm]")
        ax_di_total.set_ylabel("decay index")
        ax_di_total.xaxis.set_major_locator(MultipleLocator(50))
        ax_di_total.xaxis.set_minor_locator(MultipleLocator(10))
        ax_di_total.yaxis.set_major_locator(MultipleLocator(1))
        ax_di_total.yaxis.set_minor_locator(MultipleLocator(0.5))
        ax_di_total.grid()


        self.new_synmap = self._overlay_hmi_to_syn()
        self.decay_index_list = []
        pfss_in = pfsspy.Input(self.new_synmap, self.nr, self.rss)
        self.pfss_out = pfsspy.pfss(pfss_in)
        
        b_theta = self.pfss_out.bg[:,:,:,1]
        b_phi = self.pfss_out.bg[:,:,:,0]
        # b_r = pfss_out.bg[:,:,:,2]
        bh = np.sqrt(b_theta*b_theta + b_phi*b_phi)
        h = (np.exp(self.pfss_out.grid.rg) - 1)[1:]

        dln_bh = np.diff(np.log(bh[:,:,1:]), axis=-1)
        dln_h = np.diff(np.log(h))
        di = -dln_bh/dln_h
        # transpose axis same as synoptic map
        # di = di.transpose(1,0,2)
        # which is the valid??
        # assume the index at solar surface to be same as the index one step above
        # di = np.pad(di, [(0,0),(0,0),(1,0)], "edge")
        # assume the index at solar surface as zero, default constant value is zero
        di = np.pad(di, [(0,0),(0,0),(1,0)], "constant")
        
        self.decay_index = di
        # self.calc_height = h
        self.height_Mm = (np.exp(self.pfss_out.grid.rg) - 1) * const.R_sun.to("Mm").value

        self.h_limited = self.height_Mm[np.where(self.height_Mm<=self.h_threshold)[0]]
        self.h_interp = np.linspace(0, math.floor(self.h_limited[-1]), 1000)

        for c in self.log['coords']:
            coord = SkyCoord(c[0]*u.arcsec, c[1]*u.arcsec, frame = self.cropped_aiamap.coordinate_frame)
        
            # calibrate the difference of observed time
            # I should solve some warnings about the difference between "solar time" "Earth time"?
            coord_heligra = coord.transform_to(sunpy.coordinates.HeliographicCarrington)
            coord_syn = solar_rotate_coordinate(coord_heligra, time=self.new_synmap.date)
            c_spix = self.new_synmap.world_to_pixel(coord_syn)

            # self.ax_click.plot_coord(coord, marker="+", linewidth=10, markersize=12, path_effects=self.effects)
            #NOMORE? # self.ax_syn.plot_coord(coord_syn, color="white", marker="+", linewidth=5, markersize=10)

            di_click_point = self.click_point_decay(c_spix.x.value, c_spix.y.value)
            # di_one, h_one = self.interp_decay(di, , height, h_limit)
            for i in range(di_click_point.shape[0]):
                for j in range(di_click_point.shape[1]):
                    self.decay_index_list.append(di_click_point[i,j])
            ax_di_each.plot(self.h_interp, self.interp_decay(np.average(di_click_point, axis=(0,1)))[0])

        di_ave = np.average(np.array(self.decay_index_list), axis=0)
        di_std = np.std(np.array(self.decay_index_list), axis=0)
        di_interp, key_height = self.interp_decay(di_ave)
        ax_di_total.vlines(x=key_height, ymin=0, ymax=1.5, color="red")
        ax_di_total.text(key_height, 2.5, f"h_crit = {key_height:.1f} Mm", horizontalalignment="center", color="red")
        ax_di_total.plot(self.h_interp, di_interp, color="black")
        ax_di_total.errorbar(self.h_limited, di_ave, yerr=di_std, fmt="none", ecolor="black", elinewidth=1)

        fig_di_each.savefig(f"{imgpath}each.pdf")
        fig_di_total.savefig(f"{imgpath}total.pdf")

    # plot magnetic lines on the solar surface
    def plotmlines(self):
        c_lon, c_lat = map(int, self.log['crop_area']['center_coord'])
        height = int(self.log['crop_area']['height'])
        width = int(self.log['crop_area']['width'])

        self.blc_la = SkyCoord(Tx=(c_lon-int(width*2))*u.arcsec,Ty=(c_lat-int(height*2))*u.arcsec,frame=self.new_aiamap.coordinate_frame)
        self.trc_la = SkyCoord(Tx=(c_lon+int(width*2))*u.arcsec,Ty=(c_lat+int(height*2))*u.arcsec,frame=self.new_aiamap.coordinate_frame)
        self.blc_lh = SkyCoord(Tx=(c_lon-int(width*2))*u.arcsec,Ty=(c_lat-int(height*2))*u.arcsec,frame=self.new_hmimap.coordinate_frame)
        self.trc_lh = SkyCoord(Tx=(c_lon+int(width*2))*u.arcsec,Ty=(c_lat+int(height*2))*u.arcsec,frame=self.new_hmimap.coordinate_frame)
        # print(blc, trc)
        self.cropped_large_aiamap = self.new_aiamap.submap(self.blc_la, top_right=self.trc_la)
        self.cropped_large_hmimap = self.new_hmimap.submap(self.blc_lh, top_right=self.trc_lh)

        hp_lon = np.linspace(c_lon-int(width/2), c_lon+int(width/2), 5) * u.arcsec
        hp_lat = np.linspace(c_lat-int(height/2), c_lat+int(height/2), 5) * u.arcsec
        lon, lat = np.meshgrid(hp_lon, hp_lat)
        # seeds = SkyCoord(lon.ravel(), lat.ravel(), frame=self.cropped_large_aiamap.coordinate_frame)
        seeds = SkyCoord(lon.ravel(), lat.ravel(), frame=self.new_aiamap.coordinate_frame)

        self.new_synmap = self._overlay_hmi_to_syn()
        pfss_in = pfsspy.Input(self.new_synmap, self.nr, self.rss)
        self.pfss_out = pfsspy.pfss(pfss_in)

        tracer = tracing.FortranTracer(20000, 0.01)
        flines = tracer.trace(seeds, self.pfss_out)

        plt.rcParams["font.family"] = "Arial"
        plt.rcParams["font.size"] = 20

        fig = plt.figure(figsize=(9,9))
        # ax_mline = plt.subplot(1, 1, 1, projection=self.cropped_large_aiamap)
        # self.cropped_large_aiamap.plot(axes=ax_mline, clip_interval=(1, 99.99)*u.percent)
        # grid = self.cropped_large_aiamap.draw_grid()
        ax_mline = plt.subplot(1, 1, 1, projection=self.new_aiamap)
        self.new_aiamap.plot(axes=ax_mline, clip_interval=(1, 99.99)*u.percent)
        # grid = self.new_aiamap.draw_grid()

        # levels = [-1000, -100, 100, 1000] * u.Gauss
        # levels = [-500, -100, 100, 500] * u.Gauss
        # levels = [-50, 50] * u.Gauss
        # cset = self.cropped_large_hmimap.draw_contours(levels, axes=ax_mline, cmap="bwr", alpha=0.7)

        for fline in flines:
            ax_mline.plot_coord(fline.coords, alpha=0.8, linewidth=1, color='white')
        plt.show()


    def click_point_decay(self, x, y): # > onclick ?
        # get decay indecies at the nearest 4 grid points 
        di_limited = self.decay_index[:,:,np.where(self.height_Mm <= self.h_threshold)[0]]
        # di_averaged = np.average(di_limited[math.floor(y):math.floor(y)+2,math.floor(x):math.floor(x)+2], axis=(0,1))
        return di_limited[math.floor(x):math.floor(x)+2,math.floor(y):math.floor(y)+2]

    def interp_decay(self, di_in_question):
        f = interpolate.interp1d(self.h_limited, di_in_question, kind="cubic")
        di_interp = f(self.h_interp)
        f2 = lambda x: f(x) - self.key_di
        key_height = optimize.newton(f2, 1)
        return di_interp, key_height

    def preplot(self):
        fig_pre = plt.figure(figsize=(9,9))
        ax_pre = fig_pre.add_subplot(1,1,1, projection=self.new_aiamap)
        cid = fig_pre.canvas.mpl_connect('button_press_event', self._onclick_preplot)
        self.new_aiamap.plot(axes = ax_pre, clip_interval = (1, 99.99)*u.percent)
        # levels = [-50, 50] * u.Gauss
        # cset = self.new_hmimap.draw_contours(levels, axes=ax_pre, cmap="bwr", alpha=0.7)
        plt.show()
        plt.close()

        c_lon = int(self.center_coord.Tx.value)
        c_lat = int(self.center_coord.Ty.value)

        print(f"last clicked coordinates are x:{c_lon} y:{c_lat}")
        print("please input the height of cropping (unit: arcsec)")
        height = int(input())
        print("please input the width of cropping (unit: arcsec)")
        width = int(input())
        # height, width = 500, 500 # DEBUG

        self.log['crop_area'] = {
            'center_coord': [self.center_coord.Tx.value, self.center_coord.Ty.value],
            'height': height,
            'width': width }

        fig_crop = plt.figure(figsize=(9,9))
        self.blc_a = SkyCoord(Tx=(c_lon-int(width/2))*u.arcsec,Ty=(c_lat-int(height/2))*u.arcsec,frame=self.new_aiamap.coordinate_frame)
        self.trc_a = SkyCoord(Tx=(c_lon+int(width/2))*u.arcsec,Ty=(c_lat+int(height/2))*u.arcsec,frame=self.new_aiamap.coordinate_frame)
        self.blc_h = SkyCoord(Tx=(c_lon-int(width/2))*u.arcsec,Ty=(c_lat-int(height/2))*u.arcsec,frame=self.new_hmimap.coordinate_frame)
        self.trc_h = SkyCoord(Tx=(c_lon+int(width/2))*u.arcsec,Ty=(c_lat+int(height/2))*u.arcsec,frame=self.new_hmimap.coordinate_frame)
        # print(blc, trc)
        self.cropped_aiamap = self.new_aiamap.submap(self.blc_a, top_right=self.trc_a)
        self.cropped_hmimap = self.new_hmimap.submap(self.blc_h, top_right=self.trc_h)
        ax_crop = fig_crop.add_subplot(1,1,1, projection = self.cropped_aiamap)
        self.cropped_aiamap.plot()
        levels = [-50, 50] * u.Gauss
        cset = self.cropped_hmimap.draw_contours(levels, axes=ax_crop, cmap="bwr", alpha=0.7)
        plt.show()
        
    def select_coordinates(self):
        exit = True
        while exit:
            self.preplot()
            print("if you finish and go next, input 'y' (if repeat, input some other key):")
            pressed = str(input()).lower()
            if pressed == "y":
                exit = False

    def savelog(self, logpath):
        with open(logpath, 'w') as f:
            json.dump(self.log, f, indent=4)

    def loadlog(self, logpath):
        with open(logpath) as f:
            self.log = json.load(f)
        self.h_threshold = 200 #self.log['h_threshold']
        self.key_di = self.log['key_di']
        self.set_local_path(self.log['aia_path'], self.log['hmi_path'], self.log['syn_path'], downsample=True)

        c_lon, c_lat = map(int, self.log['crop_area']['center_coord'])

        height = int(self.log['crop_area']['height'])
        width = int(self.log['crop_area']['width'])

        self.blc_a = SkyCoord(Tx=(c_lon-int(width/2))*u.arcsec,Ty=(c_lat-int(height/2))*u.arcsec,frame=self.new_aiamap.coordinate_frame)
        self.trc_a = SkyCoord(Tx=(c_lon+int(width/2))*u.arcsec,Ty=(c_lat+int(height/2))*u.arcsec,frame=self.new_aiamap.coordinate_frame)
        self.blc_h = SkyCoord(Tx=(c_lon-int(width/2))*u.arcsec,Ty=(c_lat-int(height/2))*u.arcsec,frame=self.new_hmimap.coordinate_frame)
        self.trc_h = SkyCoord(Tx=(c_lon+int(width/2))*u.arcsec,Ty=(c_lat+int(height/2))*u.arcsec,frame=self.new_hmimap.coordinate_frame)
        # print(blc, trc)
        self.cropped_aiamap = self.new_aiamap.submap(self.blc_a, top_right=self.trc_a)
        self.cropped_hmimap = self.new_hmimap.submap(self.blc_h, top_right=self.trc_h)

    
if __name__ == '__main__':
    nr, rss = 100, 2
    h_limit = 200 # Mm
    key_di = 1.5

    hpath = "/Users/kihara/sunpy/data/hmi_m_45s_2017_07_14_01_01_30_tai_magnetogram.fits"
    apath = "/Users/kihara/sunpy/data/aia_lev1_1600a_2017_07_14t01_03_50_12z_image_lev1.fits"
    spath = "/Users/kihara/sunpy/data/hmi.synoptic_mr_polfil_720s.2192.Mr_polfil.fits"

    logpath = "/Users/kihara/Files/research/02_SEP_CDAW/SEP_D/for_paper/di_coords/191_memo.json"

    DIC = DecayIdxCalculator(nr, rss)
    # DIC.set_fido_file(1600, t_aia, t_hmi, downsample=False, showfilename=True)
    # DIC.set_local_path(apath, hpath, spath, downsample=False)
    # DIC.select_coordinates()
    # DIC.cal_decay(h_threshold = h_limit, key_di = key_di)
    # print(DIC.log)
    # DIC.savelog(logpath)
    DIC.loadlog(logpath)
    # imgpath = "/Users/kihara/Files/research/02_SEP_CDAW/SEP_D/for_paper/paper_figs/fig6/024_"
    DIC.cal_decay_ss(h_threshold = h_limit, key_di = key_di)
    # DIC.plotlog(imgpath)
    # DIC.plotmlines()


