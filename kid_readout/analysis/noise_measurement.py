import numpy as np
import matplotlib
matplotlib.use('agg')
#matplotlib.rcParams['mathtext.fontset'] = 'stix'
matplotlib.rcParams['font.size'] = 16.0
from matplotlib import pyplot as plt
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
mlab = plt.mlab

from kid_readout.analysis.resonator import Resonator,fit_best_resonator
from kid_readout.analysis import iqnoise
from kid_readout.utils import readoutnc

#from kid_readout.utils.fftfilt import fftfilt
from kid_readout.utils.filters import low_pass_fir

from kid_readout.utils.despike import deglitch_window

import socket
if socket.gethostname() == 'detectors':
    from kid_readout.utils.hpd_temps import get_temperatures_at
else:
    from kid_readout.utils.starcryo_temps import get_temperatures_at
import time
import os
import glob
from kid_readout.analysis.resources import experiments

import cPickle


def plot_noise_nc(fglob,**kwargs):
    if type(fglob) is str:
        fnames = glob.glob(fglob)
    else:
        fnames = fglob
    try:
        plotall = kwargs.pop('plot_all')
    except KeyError:
        plotall = False
    fnames.sort()
    errors = {}
    pdf = None
    for fname in fnames:
        try:
            fdir,fbase = os.path.split(fname)
            fbase,ext = os.path.splitext(fbase)
            rnc = readoutnc.ReadoutNetCDF(fname)
            nms = []
            for (k,((sname,swg),(tname,tsg))) in enumerate(zip(rnc.sweeps_dict.items(),rnc.timestreams_dict.items())):
                #fig = plot_noise(swg,tsg,hwg,chip,**kwargs)
                indexes = np.unique(swg.index)
                for index in indexes:
                    try:
                        nm = SweepNoiseMeasurement(fname,sweep_group_index=k,timestream_group_index=k,
                                                   resonator_index=index,**kwargs)
                    except IndexError:
                        print "failed to find index",index,"in",sname,tname
                        continue

                    if plotall or k == 0:
                        if pdf is None:
                            chipfname = nm.chip_name.replace(' ','_').replace(',','')
                            pdf = PdfPages('/home/data/plots/%s_%s.pdf' % (fbase,chipfname))

                        fig = Figure(figsize=(16,8))
                        title = ('%s %s' % (sname,tname))
                        nm.plot(fig=fig,title=title)
                        canvas = FigureCanvasAgg(fig)
                        fig.set_canvas(canvas)
                        pdf.savefig(fig,bbox_inches='tight')
                    else:
                        if pdf is not None:
                            pdf.close()
                            pdf = None
                    nms.append(nm)
                    
                print fname,nm.start_temp,"K"
            if pdf is not None:
                pdf.close()
            rnc.close()
            fh = open(os.path.join('/home/data','noise_sweeps_' +fbase+'.pkl'),'w')
            cPickle.dump(nms,fh,-1)
            fh.close()
        except Exception,e:
            raise
            errors[fname] = e
    return errors

class SweepNoiseMeasurement(object):
    def __init__(self,sweep_filename,sweep_group_index=0,timestream_filename=None,timestream_group_index=0,
                 resonator_index=0,low_pass_cutoff_Hz=4.0,
                 dac_chain_gain = -52, ntones=None, use_bifurcation=False, delay_estimate=-7.29,
                 deglitch_threshold=5, cryostat=None):
        
        self.sweep_filename = sweep_filename
        self.timestream_filename = timestream_filename
        self.sweep_group_index = sweep_group_index
        self.timestream_group_index = timestream_group_index
        self._open_netcdf_files()
        
        self.sweep_epoch = self.sweep.start_epoch
        pkg1,pkg2,load1,load2 = get_temperatures_at(self.sweep.start_epoch)
        self.primary_package_temperature_during_sweep = pkg1
        self.secondary_package_temperature_during_sweep = pkg2
        self.primary_load_temperature_during_sweep = load1
        self.secondary_load_temperature_during_sweep = load2
        self.start_temp = self.primary_package_temperature_during_sweep
        self.resonator_index = resonator_index
        
        description,is_dark,optical_load = experiments.get_experiment_info_at(self.sweep_epoch, cryostat=cryostat)
        self.chip_name = description
        self.is_dark = is_dark
        self.optical_load = optical_load
        self.dac_chain_gain = dac_chain_gain
        
        try:
            self.atten, self.total_dac_atten = self.sweep_file.get_effective_dac_atten_at(self.sweep_epoch)
            self.power_dbm = dac_chain_gain - self.total_dac_atten
        except:
            print "failed to find attenuator settings"
            self.atten = np.nan
            self.total_dac_atten = np.nan
            self.power_dbm = np.nan
            
        self.sweep_freqs_MHz, self.sweep_s21, self.sweep_errors = self.sweep.select_by_index(resonator_index)
        
        # find the time series that was measured closest to the sweep frequencies
        # this is a bit sloppy...
        timestream_index = np.argmin(abs(self.timestream.measurement_freq-self.sweep_freqs_MHz.mean()))
        self.timestream_index = timestream_index
        
        original_timeseries = self.timestream.get_data_index(timestream_index)
        self.adc_sampling_freq_MHz = self.timestream.adc_sampling_freq[timestream_index]
        self.noise_measurement_freq_MHz = self.timestream.measurement_freq[timestream_index]
        self.nfft = self.timestream.nfft[timestream_index]
        self.timeseries_sample_rate_Hz = self.timestream.sample_rate[timestream_index]
        
        self.timestream_epoch = self.timestream.epoch[timestream_index]
        self.timestream_duration = original_timeseries.shape[0]/self.timeseries_sample_rate_Hz
        # The following hack helps fix a long standing timing bug which was recently fixed/improved
        if self.timestream_epoch < 1399089567:
            self.timestream_epoch -= self.timestream_duration
        # end hack
        self.timestream_temperatures_sample_times = np.arange(self.timestream_duration)
        pkg1,pkg2,load1,load2 = get_temperatures_at(self.timestream_epoch + self.timestream_temperatures_sample_times)
        self.primary_package_temperature_during_timestream = pkg1
        self.secondary_package_temperature_during_timestream = pkg2
        self.primary_load_temperature_during_timestream = load1
        self.secondary_load_temperature_during_timestream = load2
        self.end_temp = self.primary_package_temperature_during_timestream[-1]

        
        # We can use the timestream measurement as an additional sweep point.
        # We average only the first 2048 points of the timeseries to avoid any drift. 
        self.sweep_freqs_MHz = np.hstack((self.sweep_freqs_MHz,[self.noise_measurement_freq_MHz]))
        self.sweep_s21 = np.hstack((self.sweep_s21,[original_timeseries[:2048].mean()]))
        self.sweep_errors = np.hstack((self.sweep_errors,
                                           [original_timeseries[:2048].real.std()/np.sqrt(2048)
                                            +original_timeseries[:2048].imag.std()/np.sqrt(2048)]))
        
        # Now put all the sweep data in increasing frequency order so it plots nicely
        order = self.sweep_freqs_MHz.argsort()
        self.sweep_freqs_MHz = self.sweep_freqs_MHz[order]
        self.sweep_s21 = self.sweep_s21[order]
        self.sweep_errors = self.sweep_errors[order]
        
        rr = fit_best_resonator(self.sweep_freqs_MHz,self.sweep_s21,errors=self.sweep_errors,delay_estimate=delay_estimate)
        self.resonator_model = rr
        self.Q_i = rr.Q_i
        self.fit_params = rr.result.params
        
        decimation_factor = self.timeseries_sample_rate_Hz/low_pass_cutoff_Hz
        normalized_timeseries = rr.normalize(self.noise_measurement_freq_MHz,original_timeseries)
        self.low_pass_normalized_timeseries = low_pass_fir(normalized_timeseries, num_taps=1024, cutoff=low_pass_cutoff_Hz, 
                                                          nyquist_freq=self.timeseries_sample_rate_Hz, decimate_by=decimation_factor)
        self.normalized_timeseries_mean = normalized_timeseries.mean()

        projected_timeseries = rr.project_s21_to_delta_freq(self.noise_measurement_freq_MHz,normalized_timeseries,
                                                            s21_already_normalized=True)
        
        # calculate the number of samples for the deglitching window.
        # the following will be the next power of two above 1 second worth of samples
        window = int(2**np.ceil(np.log2(self.timeseries_sample_rate_Hz)))
        # reduce the deglitching window if we don't have enough samples
        if window > projected_timeseries.shape[0]:
            window = projected_timeseries.shape[0]//2
        self.deglitch_window = window
        self.deglitch_threshold = deglitch_threshold
        deglitched_timeseries = deglitch_window(projected_timeseries,window,thresh=deglitch_threshold)
        
        
        self.low_pass_projected_timeseries = low_pass_fir(deglitched_timeseries, num_taps=1024, cutoff=low_pass_cutoff_Hz, 
                                                nyquist_freq=self.timeseries_sample_rate_Hz, decimate_by=decimation_factor)
        self.low_pass_timestep = decimation_factor/self.timeseries_sample_rate_Hz
        
        self.normalized_model_s21_at_meas_freq = rr.normalized_model(self.noise_measurement_freq_MHz)
        self.normalized_model_s21_at_resonance = rr.normalized_model(rr.f_0)
        self.normalized_ds21_df_at_meas_freq = rr.approx_normalized_gradient(self.noise_measurement_freq_MHz)
        
        self.sweep_normalized_s21 = rr.normalize(self.sweep_freqs_MHz,self.sweep_s21)

        self.sweep_model_freqs_MHz = np.linspace(self.sweep_freqs_MHz.min(),self.sweep_freqs_MHz.max(),1000)
        self.sweep_model_normalized_s21 = rr.normalized_model(self.sweep_model_freqs_MHz) 
        self.sweep_model_normalized_s21_centered = self.sweep_model_normalized_s21 - self.normalized_timeseries_mean
        
        fractional_fluctuation_timeseries = deglitched_timeseries / (self.noise_measurement_freq_MHz*1e6)
        self._fractional_fluctuation_timeseries = fractional_fluctuation_timeseries
        fr,S,evals,evects,angles,piq = iqnoise.pca_noise(fractional_fluctuation_timeseries, 
                                                         NFFT=None, Fs=self.timeseries_sample_rate_Hz)
        
        self.pca_freq = fr
        self.pca_S = S
        self.pca_evals = evals
        self.pca_evects = evects
        self.pca_angles = angles
        self.pca_piq = piq
        
        self.freqs_coarse,self.prr_coarse,self.pii_coarse = self.get_projected_fractional_fluctuation_spectra(NFFT=2**12)
        
        self._normalized_timeseries = normalized_timeseries[:2048].copy()
        
    @property
    def original_timeseries(self):
        return self.timestream.get_data_index(self.timestream_index)
    
    @property
    def normalized_timeseries(self):
        return self.resonator_model.normalize(self.noise_measurement_freq_MHz,self.original_timeseries)
    
    @property
    def projected_timeseries(self):
        return self.resonator_model.project_s21_to_delta_freq(self.noise_measurement_freq_MHz,self.normalized_timeseries,
                                                            s21_already_normalized=True)
    
    @property
    def fractional_fluctuation_timeseries(self):
        if self._fractional_fluctuation_timeseries is None:
            self._fractional_fluctuation_timeseries = self.get_deglitched_timeseries()/(self.noise_measurement_freq_MHz*1e6)
        return self._fractional_fluctuation_timeseries
        
    def get_deglitched_timeseries(self,window_in_seconds=1.0, thresh=None):
        # calculate the number of samples for the deglitching window.
        # the following will be the next power of two above 1 second worth of samples
        window = int(2**np.ceil(np.log2(window_in_seconds*self.timeseries_sample_rate_Hz)))
        # reduce the deglitching window if we don't have enough samples
        projected_timeseries = self.projected_timeseries
        if window > projected_timeseries.shape[0]:
            window = projected_timeseries.shape[0]//2
            
        if thresh is None:
            thresh = self.deglitch_threshold

        deglitched_timeseries = deglitch_window(projected_timeseries,window,thresh=thresh)
        return deglitched_timeseries
    
    def get_projected_fractional_fluctuation_spectra(self,NFFT=2**12,window=mlab.window_none):
        prr,freqs = mlab.psd(self.fractional_fluctuation_timeseries.real,NFFT=NFFT,
                                                           window=window,Fs=self.timeseries_sample_rate_Hz)
        pii,freqs = mlab.psd(self.fractional_fluctuation_timeseries.imag,NFFT=NFFT,
                                                           window=window,Fs=self.timeseries_sample_rate_Hz)
        return freqs,prr,pii

    
    def __getstate__(self):
        d = self.__dict__.copy()
        del d['sweep_file']
        del d['timestream_file']
        del d['sweep']
        del d['timestream']
        del d['resonator_model']
        d['_fractional_fluctuation_timeseries'] = None
        return d
        
    def __setstate__(self,state):
        self.__dict__ = state
        try:
            self._open_netcdf_files()
        except IOError:
            print "Warning: could not open associated NetCDF datafiles when unpickling."
            print "Some features of the class will not be available"
        try:
            self._restore_resonator_model()
        except Exception, e:
            print "error while restoring resonator model:",e
            
    def _open_netcdf_files(self):
        self.sweep_file = readoutnc.ReadoutNetCDF(self.sweep_filename)
        if self.timestream_filename is not None:
            self.timestream_file = readoutnc.ReadoutNetCDF(self.timestream_filename)
        else:
            self.timestream_file = self.sweep_file
        self.sweep = self.sweep_file.sweeps[self.sweep_group_index]
        self.timestream = self.timestream_file.timestreams[self.timestream_group_index]  
        
    def _restore_resonator_model(self):
        self.resonator_model = fit_best_resonator(self.sweep_freqs_MHz,self.sweep_s21,errors=self.sweep_errors,
                                                  delay_estimate=self.fit_params['delay'].value)

    def plot(self,fig=None,title=''):
        if fig is None:
            f1 = plt.figure(figsize=(16,8))
        else:
            f1 = fig
        ax1 = f1.add_subplot(121)
        ax2 = f1.add_subplot(222)
        ax2b = ax2.twinx()
        ax2b.set_yscale('log')
        ax3 = f1.add_subplot(224)
        f1.subplots_adjust(hspace=0.25)
        
        ax1.plot((self.sweep_normalized_s21).real,(self.sweep_normalized_s21).imag,'.-',lw=2,label='measured frequency sweep')
        ax1.plot(self.sweep_model_normalized_s21.real,self.sweep_model_normalized_s21.imag,'.-',markersize=2,label='model frequency sweep')
        ax1.plot([self.normalized_model_s21_at_resonance.real],[self.normalized_model_s21_at_resonance.imag],'kx',mew=2,markersize=20,label='model f0')
        ax1.plot([self.normalized_timeseries_mean.real],[self.normalized_timeseries_mean.imag],'m+',mew=2,markersize=20,label='timeseries mean')
        ax1.plot(self._normalized_timeseries.real[:128],self._normalized_timeseries.imag[:128],'k,',alpha=1,label='timeseries samples')
        ax1.plot(self.low_pass_normalized_timeseries.real,self.low_pass_normalized_timeseries.imag,'r,') #uses proxy for label
        #ax1.plot(self.pca_evects[0,0,:100]*100,self.pca_evects[1,0,:100]*100,'y.')
        #ax1.plot(self.pca_evects[0,1,:100]*100,self.pca_evects[1,1,:100]*100,'k.')
        x1 = self.normalized_model_s21_at_meas_freq.real
        y1 = self.normalized_model_s21_at_meas_freq.imag
        x2 = x1 + self.normalized_ds21_df_at_meas_freq.real*100
        y2 = y1 + self.normalized_ds21_df_at_meas_freq.imag*100
        ax1.annotate("",xytext=(x1,y1),xy=(x2,y2),arrowprops=dict(lw=2,color='orange',arrowstyle='->'),zorder=0)
        #proxies
        l = plt.Line2D([0,0.1],[0,0.1],color='orange',lw=2)
        l2 = plt.Line2D([0,0.1],[0,0.1],color='r',lw=2)
        ax1.text((self.sweep_normalized_s21).real[0],(self.sweep_normalized_s21).imag[0],('%.3f kHz' % ((self.sweep_freqs_MHz[0]-self.noise_measurement_freq_MHz)*1000)))
        ax1.text((self.sweep_normalized_s21).real[-1],(self.sweep_normalized_s21).imag[-1],('%.3f kHz' % ((self.sweep_freqs_MHz[-1]-self.noise_measurement_freq_MHz)*1000)))
        ax1.set_xlim(0,1.1)
        ax1.set_ylim(-.55,.55)
        ax1.grid()
        handles,labels = ax1.get_legend_handles_labels()
        handles.append(l)
        labels.append('dS21/(100Hz)')
        handles.append(l2)
        labels.append('LPF timeseries')
        ax1.legend(handles,labels,prop=dict(size='xx-small'))
        
        ax1b = inset_axes(parent_axes=ax1, width="20%", height="20%", loc=4)
        ax1b.plot(self.sweep_freqs_MHz,20*np.log10(abs(self.sweep_normalized_s21)),'.-')
        frm = np.linspace(self.sweep_freqs_MHz.min(),self.sweep_freqs_MHz.max(),1000)
        ax1b.plot(frm,20*np.log10(abs(self.sweep_model_normalized_s21)))
                
        freqs_fine,prr_fine,pii_fine = self.get_projected_fractional_fluctuation_spectra(NFFT=2**18)
        ax2.loglog(freqs_fine[1:],prr_fine[1:],'b',label='Srr')
        ax2.loglog(freqs_fine[1:],pii_fine[1:],'g',label='Sii')
        ax2.loglog(self.freqs_coarse[1:],self.prr_coarse[1:],'y',lw=2)
        ax2.loglog(self.freqs_coarse[1:],self.pii_coarse[1:],'m',lw=2)
        ax2.loglog(self.pca_freq[1:],self.pca_evals[:,1:].T,'k',lw=2)
        ax2.set_title(title,fontdict=dict(size='small'))
        
        n500 = self.prr_coarse[np.abs(self.freqs_coarse-500).argmin()]*(self.noise_measurement_freq_MHz*1e6)**2
        ax2b.annotate(("%.2g Hz$^2$/Hz @ 500 Hz" % n500),xy=(500,n500),xycoords='data',xytext=(5,20),textcoords='offset points',
                     arrowprops=dict(arrowstyle='->'))
        
        ax2b.set_xscale('log')
    #    ax2b.set_xlim(ax2.get_xlim())
        ax2.grid()
        ax2b.grid(color='r')
        ax2.set_xlim(self.pca_freq[1],self.pca_freq[-1])
        ax2.set_ylabel('1/Hz')
        ax2.set_xlabel('Hz')
        ax2.legend(prop=dict(size='small'))
        
        tsl = self.low_pass_projected_timeseries
        tsl = tsl - tsl.mean()
        dtl = self.low_pass_timestep
        t = dtl*np.arange(len(tsl))
        ax3.plot(t,tsl.real,'b',lw=2,label = 'LPF timeseries real')
        ax3.plot(t,tsl.imag,'g',lw=2,label = 'LPF timeseries imag')
        load_fluctuations_mK = (self.primary_load_temperature_during_timestream - self.primary_load_temperature_during_timestream.mean())*1000.0
        ax3.plot(self.timestream_temperatures_sample_times,load_fluctuations_mK,'r',lw=2)
        ax3.set_ylabel('Hz')
        ax3.set_xlabel('seconds')
        ax3.legend(prop=dict(size='xx-small'))
        
        params = self.fit_params
        amp_noise_voltsrthz = np.sqrt(4*1.38e-23*4.0*50)
        vread = np.sqrt(50*10**(self.power_dbm/10.0)*1e-3)
        alpha = 1.0
        Qe = abs(params['Q_e_real'].value+1j*params['Q_e_imag'].value)
        f0_dVdf = 4*vread*alpha*params['Q'].value**2/Qe
        expected_amp_noise = (amp_noise_voltsrthz/f0_dVdf)**2 
        text = (("measured at: %.6f MHz\n" % self.noise_measurement_freq_MHz)
                + ("temperature: %.1f - %.1f mK\n" %(self.start_temp*1000, self.end_temp*1000))
                + ("power: ~%.1f dBm (%.1f dB att)\n" %(self.power_dbm,self.atten))
                + ("fit f0: %.6f +/- %.6f MHz\n" % (params['f_0'].value,params['f_0'].stderr))
                + ("Q: %.1f +/- %.1f\n" % (params['Q'].value,params['Q'].stderr))
                + ("Re(Qe): %.1f +/- %.1f\n" % (params['Q_e_real'].value,params['Q_e_real'].stderr))
                + ("|Qe|: %.1f\n" % (Qe))
                + ("Qi: %.1f\n" % (self.Q_i))
                + ("Eamp: %.2g 1/Hz" % expected_amp_noise)
                )
        if expected_amp_noise > 0:
            ax2.axhline(expected_amp_noise,linewidth=2,color='m')
            ax2.text(10,expected_amp_noise,r"expected amp noise",va='top',ha='left',fontdict=dict(size='small'))
#        ax2.axhline(expected_amp_noise*4,linewidth=2,color='m')
#        ax2.text(10,expected_amp_noise*4,r"$\alpha = 0.5$",va='top',ha='left',fontdict=dict(size='small'))
        if params.has_key('a'):
            text += ("\na: %.3g +/- %.3g" % (params['a'].value,params['a'].stderr))

        ylim = ax2.get_ylim()
        ax2b.set_ylim(ylim[0]*(self.noise_measurement_freq_MHz*1e6)**2,ylim[1]*(self.noise_measurement_freq_MHz*1e6)**2)
        ax2b.set_ylabel('$Hz^2/Hz$')

        
        ax1.text(0.02,0.95,text,ha='left',va='top',bbox=dict(fc='white',alpha=0.6),transform = ax1.transAxes,
                 fontdict=dict(size='x-small'))
        
        title = ("%s\nmeasured %s\nplotted %s" % (self.chip_name,time.ctime(self.sweep_epoch),time.ctime()))
        ax1.set_title(title,size='small')
        return f1
        
def load_noise_pkl(pklname):
    fh = open(pklname,'r')
    pkl = cPickle.load(fh)
    fh.close()
    return pkl

def save_noise_pkl(pklname,obj):
    fh = open(pklname,'w')
    cPickle.dump(obj,fh,-1)
    fh.close()

