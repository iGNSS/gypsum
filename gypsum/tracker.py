import functools
import logging
from typing import Tuple

import math
from dataclasses import dataclass
from enum import Enum, auto

import numpy as np

from gypsum.antenna_sample_provider import Seconds
from gypsum.constants import SAMPLES_PER_PRN_TRANSMISSION, SAMPLES_PER_SECOND
from gypsum.satellite import GpsSatellite
from gypsum.utils import (
    AntennaSamplesSpanningOneMs,
    CarrierWavePhaseInRadians,
    DopplerShiftHz,
    PrnCodePhaseInSamples,
    frequency_domain_correlation,
)
from gypsum.utils import CorrelationProfile

_logger = logging.getLogger(__name__)


class BitValue(Enum):
    UNKNOWN = auto()
    ZERO = auto()
    ONE = auto()

    @classmethod
    def from_val(cls, val: int) -> 'BitValue':
        return {
            0: BitValue.ZERO,
            1: BitValue.ONE,
        }[val]

    def as_val(self) -> int:
        if self == BitValue.UNKNOWN:
            raise ValueError(f'Cannot convert an unknown bit value into an integer')

        return {
            BitValue.ZERO: 0,
            BitValue.ONE: 1,
        }[self]

    def inverted(self) -> 'BitValue':
        if self == BitValue.UNKNOWN:
            raise ValueError(f'Cannot invert an unknown bit value')

        return {
            BitValue.ZERO: BitValue.ONE,
            BitValue.ONE: BitValue.ZERO,
        }[self]

    def __eq__(self, other) -> bool:
        if not isinstance(other, BitValue):
            return False
        return self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)


import matplotlib.pyplot as plt


class NavigationBitPseudosymbol(Enum):
    MINUS_ONE = auto()
    ONE = auto()

    @classmethod
    def from_val(cls, val: int) -> 'NavigationBitPseudosymbol':
        return {
            -1: NavigationBitPseudosymbol.MINUS_ONE,
            1: NavigationBitPseudosymbol.ONE,
        }[val]

    def as_val(self) -> int:
        return {
            NavigationBitPseudosymbol.MINUS_ONE: -1,
            NavigationBitPseudosymbol.ONE: 1,
        }[self]


@dataclass
class GpsSatelliteTrackingParameters:
    satellite: GpsSatellite
    current_doppler_shift: DopplerShiftHz
    current_carrier_wave_phase_shift: CarrierWavePhaseInRadians
    current_prn_code_phase_shift: PrnCodePhaseInSamples

    doppler_shifts: list[DopplerShiftHz]
    carrier_wave_phases: list[CarrierWavePhaseInRadians]
    carrier_wave_phase_errors: list[float]
    navigation_bit_pseudosymbols: list[int]


def butter_filter(cutoff, fs, filter_type: str, order=5):
    from scipy.signal import butter
    return butter(order, cutoff, fs=fs, btype=filter_type, analog=False)


def butter_lowpass_filter(data, cutoff, fs, order=5):
    b, a = butter_filter(cutoff, fs, "lowpass", order=order)
    from scipy.signal import lfilter
    y = lfilter(b, a, data)
    return y


def butter_highpass_filter(data, cutoff, fs, order=5):
    b, a = butter_filter(cutoff, fs, "highpass", order=order)
    from scipy.signal import lfilter
    y = lfilter(b, a, data)
    return y


class GpsSatelliteTracker:
    def __init__(self, tracking_params: GpsSatelliteTrackingParameters, loop_bandwidth: float) -> None:
        self.tracking_params = tracking_params
        self.loop_bandwidth = loop_bandwidth
        # Common choice for zeta, considered optimal
        damping_factor = math.sqrt(2) / 2.0
        # Natural frequency
        natural_freq = loop_bandwidth / (damping_factor * (1 + damping_factor**2) ** 0.5)
        # This represents the gain of *instantaneous* error correction,
        # which applies to the estimate of the carrier wave phase.
        # Also called 'alpha'.
        self.loop_gain_phase = (4 * damping_factor * natural_freq) / (
            1 + ((2 * damping_factor * natural_freq) + (natural_freq**2))
        )
        # This represents the *integrated* error correction,
        # which applies to the estimate of the Doppler shifted frequency.
        # Also called 'beta'.
        self.loop_gain_freq = (4 * (natural_freq**2)) / (
            1 + ((2 * damping_factor * natural_freq) + (natural_freq**2))
        )

        plt.ion()
        plt.autoscale(enable=True)
        if False:
            self.errors_figure = plt.figure()
            self.errors_ax = self.errors_figure.add_subplot()
            self.phase_figure = plt.figure()
            self.phase_ax = self.phase_figure.add_subplot()
            self.errors_figure.show()
            self.phase_figure.show()
        self.constellation_fig = plt.figure(figsize=(12, 9))
        gs = plt.GridSpec(3, 3, figure=self.constellation_fig)
        self.freq_ax = self.constellation_fig.add_subplot(gs[0], title="Beat Frequency (Hz)")
        self.constellation_ax = self.constellation_fig.add_subplot(gs[1], title="IQ Constellation")
        self.samples_ax = self.constellation_fig.add_subplot(gs[2], title="Samples")
        self.phase_errors_ax = self.constellation_fig.add_subplot(gs[3], title="Carrier Phase Error")
        self.i_ax = self.constellation_fig.add_subplot(gs[4], title="I")
        self.q_ax = self.constellation_fig.add_subplot(gs[5], title="Q")
        self.iq_angle_ax = self.constellation_fig.add_subplot(gs[6], title="IQ Angle")
        self.carrier_phase_ax = self.constellation_fig.add_subplot(gs[6], title="Carrier Phase")
        self.constellation_fig.show()
        self._is = []
        self._qs = []
        self.iq_angles = []
        self.phase_errors = []
        self.carrier_phases = []
        # TODO(PT): Perhaps the carrier phase estimate that comes from acquisition is way off?

        self.time_domain_for_1ms = np.arange(SAMPLES_PER_PRN_TRANSMISSION) / SAMPLES_PER_SECOND

    def _run_tracking_loop(
        self,
        seconds_since_start: Seconds,
        samples_mixed_with_prn: AntennaSamplesSpanningOneMs
    ) -> None:
        params = self.tracking_params

        # Calculate average power of the signal
        avg_power = np.mean(np.abs(samples_mixed_with_prn) ** 2)
        desired_power = 1 ** 2  # Set your desired power level
        gain = np.sqrt(desired_power / avg_power)  # Calculate AGC gain

        # Apply AGC
        #agc_applied_samples = samples_mixed_with_prn * gain
        agc_applied_samples = samples_mixed_with_prn

        filtered = butter_lowpass_filter(agc_applied_samples, params.current_doppler_shift * 1.1, SAMPLES_PER_SECOND, 1)
        self.filtered = filtered
        duration_per_sample = 1.0 / SAMPLES_PER_SECOND
        for i, sample in enumerate(samples_mixed_with_prn):
            #timestamp = seconds_since_start + (i / SAMPLES_PER_SECOND)
            timestamp = seconds_since_start + (duration_per_sample * i)
            #doppler_shifted_multiplier = np.exp(-1j * ((2 * np.pi * params.current_doppler_shift) + params.current_carrier_wave_phase_shift))
            doppler_shifted_multiplier = np.exp(-1j * ((2 * np.pi * params.current_doppler_shift * timestamp) + params.current_carrier_wave_phase_shift))
            #self.carrier = doppler_shifted_multiplier
            # TODO(PT): Ask how to relate the amplitude of the replica carrier wave to the amplitude of the received signal?
            #doppler_shifted_multiplier = np.exp(-1j * ((2 * np.pi * params.current_doppler_shift) + params.current_carrier_wave_phase_shift))
            #doppler_shifted_multiplier = np.exp(-1j * params.current_carrier_wave_phase_shift)
            #doppler_shifted_multiplier = np.square(doppler_shifted_multiplier)
            #sample = np.square(sample)
            mixed_sample = sample * doppler_shifted_multiplier
            self.mixed = mixed_sample

            I = mixed_sample.real
            Q = mixed_sample.imag
            #self._is.append(I)
            #self._qs.append(Q)
            carrier_wave_phase_error = I * Q
            #frequency_error = np.arctan2(Q, I)

            # TODO(PT): To properly convert from radians to Hz 2π×frequency (Hz)×time interval (s).
            # Update me!
            # params.current_doppler_shift += (carrier_wave_phase_error / math.tau) * 0.1
            params.current_doppler_shift += carrier_wave_phase_error * 0.5
            #params.current_doppler_shift += (frequency_error / math.tau) * 0.01
            params.current_carrier_wave_phase_shift += carrier_wave_phase_error * 0.2
            #params.current_carrier_wave_phase_shift += ((params.current_doppler_shift * math.tau) / (1.0/SAMPLES_PER_SECOND)) + (carrier_wave_phase_error * 0.009)
            #params.current_carrier_wave_phase_shift += params.current_doppler_shift + (carrier_wave_phase_error * 0.1)
            params.current_carrier_wave_phase_shift %= math.tau

    def _run_tracking_loop2(
        self,
        seconds_since_start: Seconds,
        samples_mixed_with_prn: AntennaSamplesSpanningOneMs
    ) -> None:
        params = self.tracking_params
        carrier_wave_replica = np.exp(-1j * ((2 * np.pi * params.current_doppler_shift * (self.time_domain_for_1ms + seconds_since_start)) + params.current_carrier_wave_phase_shift))
        samples_mixed_with_carrier_wave = samples_mixed_with_prn * carrier_wave_replica
        filtered = butter_lowpass_filter(samples_mixed_with_carrier_wave, params.current_doppler_shift * 1.1, SAMPLES_PER_SECOND, 1)
        #error = np.arctan2(filtered.imag, filtered.real)
        error = filtered.real * filtered.imag
        #error = butter_lowpass_filter(error, 20, SAMPLES_PER_SECOND, 1)
        #plt.ioff()
        #plt.plot(np.fft.fft(error))
        #plt.show()
        #error = np.max(error)
        #error = np.mean(error[20:])
        error = np.sum(error)
        if True:
            params.current_doppler_shift += error * 0.1
            params.current_carrier_wave_phase_shift += error * 0.4
            params.current_carrier_wave_phase_shift %= math.tau

    def _adjust_tracking_parameters_via_correlation_profile(
        self,
        correlation_peak_value: complex,
    ) -> None:
        params = self.tracking_params
        I = correlation_peak_value.real
        Q = correlation_peak_value.imag
        carrier_wave_phase_error = I * Q
        #carrier_wave_phase_error = np.arctan2(Q, I)

        params.current_doppler_shift += (carrier_wave_phase_error / math.tau) * self.loop_gain_freq
        params.current_carrier_wave_phase_shift += carrier_wave_phase_error * self.loop_gain_phase
        params.current_carrier_wave_phase_shift %= math.tau

    def _adjust_tracking_parameters_via_correlation_profile_all(
        self,
        correlation_profile: CorrelationProfile,
    ) -> None:
        params = self.tracking_params
        filtered = butter_lowpass_filter(correlation_profile, params.current_doppler_shift * 1.1, SAMPLES_PER_SECOND, 1)

        for i, sample in enumerate(filtered):
            I = sample.real
            Q = sample.imag
            carrier_wave_phase_error = I * Q
            #carrier_wave_phase_error = np.arctan2(Q, I)

            params.current_doppler_shift += (carrier_wave_phase_error / math.tau) * self.loop_gain_freq
            params.current_carrier_wave_phase_shift += carrier_wave_phase_error * self.loop_gain_phase
            params.current_carrier_wave_phase_shift %= math.tau

    def _follow_carrier_via_correlation_profile(
        self,
        correlation_profile: CorrelationProfile,
        peak: complex,
    ) -> None:
        params = self.tracking_params

        #phase_error = np.arctan2(correlation_profile.imag, correlation_profile.real)
        #peak = np.max(correlation_profile)
        #phase_error = correlation_profile.real * correlation_profile.imag
        #phase_error = peak.real * peak.imag
        #phase_error %= math.tau
        phase_error = np.angle(peak)
        #phase_error_sum = np.sum(phase_error)
        #phase_error_sum %= math.tau
        print(f'Phase error {phase_error}')
        self.phase_errors.append(phase_error)
        params.current_doppler_shift += ((phase_error / math.tau) * 0.5)
        params.current_carrier_wave_phase_shift += (phase_error * 0.1)
        params.current_carrier_wave_phase_shift %= math.tau

    def _test(self, seconds_since_start: Seconds, samples_with_prn_wiped_off: AntennaSamplesSpanningOneMs) -> None:
        params = self.tracking_params
        time_domain = self.time_domain_for_1ms + seconds_since_start
        doppler_shift_carrier = np.exp(
            -1j * ((2 * np.pi * params.current_doppler_shift * time_domain) + params.current_carrier_wave_phase_shift)
        )
        #coherent_prompt_correlation = frequency_domain_correlation(samples_with_prn_wiped_off, doppler_shift_carrier)
        #fft_freq = fftfreq(len(carrier_wave), 1 / SAMPLE_RATE)
        from scipy.fft import fft, fftfreq
        coherent_prompt_correlation = fft(samples_with_prn_wiped_off)
        fft_freq = fftfreq(len(samples_with_prn_wiped_off), 1/SAMPLES_PER_SECOND)
        non_coherent_prompt_correlation = np.abs(coherent_prompt_correlation)
        non_coherent_prompt_peak_offset = np.argmax(non_coherent_prompt_correlation)

        peak_frequency = fft_freq[non_coherent_prompt_peak_offset]
        peak_phase = np.angle(coherent_prompt_correlation[non_coherent_prompt_peak_offset])

        print(f'Peak offset {non_coherent_prompt_peak_offset:.2f}')
        print(f'Peak freq {peak_frequency:.2f}')
        print(f'Peak phase {peak_phase:.2f}')

        offset_in_rad = peak_phase

        #offset_in_rad = (non_coherent_prompt_peak_offset / 2046) * math.pi
        print(f'offset_in_rad {offset_in_rad:.2f}')

        params.current_carrier_wave_phase_shift += peak_phase
        params.current_doppler_shift += peak_phase
        if False:
            plt.ioff()
            plt.title("Carrier wave correlation")
            plt.plot(coherent_prompt_correlation, label="Coherent")
            plt.plot(non_coherent_prompt_correlation, label="Non-coherent")
            plt.legend()
            plt.show()
            plt.ion()

    def _test2(self, samples_with_carrier_and_prn_wipeoff: AntennaSamplesSpanningOneMs, sample_offset: int) -> None:
        sample = samples_with_carrier_and_prn_wipeoff[sample_offset]
        #error = np.arctan(sample.imag, sample.real)
        #error = np.angle(sample)
        error = sample.real * sample.imag
        #alpha = 0.005
        #beta = 0.0001
        alpha = 0.01
        beta = 0.0001
        self.tracking_params.current_carrier_wave_phase_shift += error * alpha
        self.tracking_params.current_carrier_wave_phase_shift %= math.tau
        self.tracking_params.current_doppler_shift += error * beta
        self.tracking_params.carrier_wave_phase_errors.append(error)
        self.iq_angles.append(np.angle(sample))
        # _logger.info(f'Error {error:.2f}, freq {self.tracking_params.current_doppler_shift:.2f}, phase {self.tracking_params.current_carrier_wave_phase_shift:.2f}')

    def _is_locked(self) -> bool:
        # The PLL currently runs at 1000Hz, so each error entry is spaced at 1ms.
        # TODO(PT): Pull this out into a constant.
        previous_milliseconds_to_consider = 250
        if len(self.tracking_params.carrier_wave_phase_errors) < previous_milliseconds_to_consider:
            # We haven't run our PLL for long enough to determine lock
            return False
        last_few_phase_errors = self.tracking_params.carrier_wave_phase_errors[-previous_milliseconds_to_consider:]
        phase_error_variance = np.var(last_few_phase_errors)
        # TODO(PT): Pull this out into a constant?
        is_phase_error_variance_under_threshold = phase_error_variance < 900

        # Default to claiming the I channel is fine if we don't have enough samples to make a proper decision
        does_i_channel_look_locked = True
        if len(self._is) > 2:
            last_few_i_values = self._is[-previous_milliseconds_to_consider:]
            #import statistics
            #s = statistics.stdev(last_few_i_values)
            s = np.var(last_few_i_values)
            # A locked `I` channel should output values strongly centered around a positive pole and a negative pole.
            # We don't know the exact values of these poles, as they'll depend on the exact signal, but we can split
            # our `I` channel into positive and negative components and try to see how strongly values are clustered
            # around each pole.
            positive_i_values = [x for x in last_few_i_values if x >= 0]
            positive_var = np.var(positive_i_values)
            negative_i_values = [x for x in last_few_i_values if x < 0]
            negative_var = np.var(negative_i_values)
            s = (positive_var + negative_var) / 2.0
            #print(f'stdev: {s:.2f}')
            # PT: Chosen through experimentation
            does_i_channel_look_locked = s < 2
            # Prev 900, 2, 6

        points = list(complex(i, q) for i, q in zip(self._is, self._qs))
        is_constellation_rotation_acceptable = True
        if len(points) > 2:
            points_on_left_pole = [p for p in points if p.real < 0]
            points_on_right_pole = [p for p in points if p.real >= 0]
            left_point = np.mean(points_on_left_pole)
            # right_point = np.mean(points_on_right_pole)
            angle = 180 - (((np.arctan2(left_point.imag, left_point.real) / math.tau) * 360) % 180)
            centered_angle = angle if angle < 90 else 180 - angle
            is_constellation_rotation_acceptable = abs(centered_angle < 6)

        #return is_phase_error_variance_under_threshold

        return (
            is_phase_error_variance_under_threshold
            and does_i_channel_look_locked
            and is_constellation_rotation_acceptable
        )

    @functools.lru_cache
    def _calculate_loop_filter_alpha_and_beta(self, loop_bandwidth: float) -> Tuple[float, float]:
        # Common choice for zeta, considered optimal
        damping_factor = math.sqrt(2) / 2.0
        # Natural frequency
        natural_freq = loop_bandwidth / (damping_factor * (1 + damping_factor**2) ** 0.5)
        # This represents the *integrated* error correction,
        # which applies to the estimate of the Doppler shifted frequency.
        # Also called 'beta'.
        loop_gain_freq = (4 * damping_factor * natural_freq) / (
                1 + ((2 * damping_factor * natural_freq) + (natural_freq**2))
        )
        # This represents the gain of *instantaneous* error correction,
        # which applies to the estimate of the carrier wave phase.
        # Also called 'alpha'.
        loop_gain_phase = (4 * (natural_freq**2)) / (
                1 + ((2 * damping_factor * natural_freq) + (natural_freq**2))
        )

        time_per_sample = 1.0 / SAMPLES_PER_SECOND
        zeta = 1.0 / math.sqrt(2)
        loop_gain_phase = 4 * zeta * loop_bandwidth * time_per_sample
        loop_gain_freq = 4 * (loop_bandwidth ** 2) * time_per_sample

        #factor = 1.0
        #return loop_gain_phase / factor, loop_gain_freq / factor
        return loop_gain_phase, loop_gain_freq

    def _test5(self, samples_with_carrier_and_prn_wipeoff: AntennaSamplesSpanningOneMs, sample) -> None:
        error = sample.real * sample.imag

        if self._is_locked():
            alpha, beta = self._calculate_loop_filter_alpha_and_beta(3)
            #alpha = 0.0005
            #beta = 0.00005
        else:
            #alpha = 0.001
            #beta = 0.0001
            alpha, beta = self._calculate_loop_filter_alpha_and_beta(6)
        #print(f'{alpha:f}, {beta:f}')
        self.tracking_params.current_carrier_wave_phase_shift += error * alpha
        self.tracking_params.current_carrier_wave_phase_shift %= math.tau
        self.tracking_params.current_doppler_shift += error * beta
        self.tracking_params.carrier_wave_phase_errors.append(error)
        self.iq_angles.append(np.angle(sample))

    def _test3(self, samples_with_carrier_and_prn_wipeoff: AntennaSamplesSpanningOneMs) -> None:
        for i, sample in enumerate(samples_with_carrier_and_prn_wipeoff):
            error = sample.real * sample.imag
            alpha = 0.001
            beta = 0.00001
            self.tracking_params.current_carrier_wave_phase_shift += error * alpha
            self.tracking_params.current_carrier_wave_phase_shift %= math.tau
            self.tracking_params.current_doppler_shift += error * beta
            if i == 0:
                self.tracking_params.carrier_wave_phase_errors.append(error)
                self.iq_angles.append(np.angle(sample))

    def _test4(self, samples_with_carrier_and_prn_wipeoff: AntennaSamplesSpanningOneMs) -> None:
        for i, sample in enumerate(samples_with_carrier_and_prn_wipeoff):
            error = sample.real * sample.imag
            alpha = 0.001
            beta = 0.00001
            self.tracking_params.current_carrier_wave_phase_shift += error * alpha
            self.tracking_params.current_carrier_wave_phase_shift %= math.tau
            self.tracking_params.current_doppler_shift += error * beta
            if i == 0:
                self.tracking_params.carrier_wave_phase_errors.append(error)
                self.iq_angles.append(np.angle(sample))

    def _test6(self, seconds_since_start, samples_with_prn_wipeoff: AntennaSamplesSpanningOneMs) -> None:
        params = self.tracking_params
        time_domain = self.time_domain_for_1ms + seconds_since_start
        doppler_shift_carrier = np.exp(
            -1j * ((2 * np.pi * params.current_doppler_shift * time_domain) + params.current_carrier_wave_phase_shift)
        )
        i_t = samples_with_prn_wipeoff * doppler_shift_carrier.real
        q_t = samples_with_prn_wipeoff * doppler_shift_carrier.imag

        i_filt = butter_lowpass_filter(i_t, params.current_doppler_shift * 1.1, SAMPLES_PER_SECOND, 1)
        q_filt = butter_lowpass_filter(q_t, params.current_doppler_shift * 1.1, SAMPLES_PER_SECOND, 1)

        phase_error = (i_t * q_filt) - (q_t * i_filt)

        print(phase_error)
        for error in phase_error:
            error = float(error)
            alpha = 0.001
            beta = 0.00001
            self.tracking_params.current_carrier_wave_phase_shift += error * alpha
            self.tracking_params.current_carrier_wave_phase_shift %= math.tau
            self.tracking_params.current_doppler_shift += error * beta

        if False:
            plt.ioff()
            plt.plot(phase_error)
            plt.show()
            plt.ion()

    def process_samples(self, seconds_since_start: Seconds, samples: AntennaSamplesSpanningOneMs) -> NavigationBitPseudosymbol:
        if (seconds_since_start % 1) == 0:
            locked_state = "Locked" if self._is_locked() else "Unlocked"
            last_few_phase_errors = self.tracking_params.carrier_wave_phase_errors[-250:]
            variance = np.var(last_few_phase_errors)
            print(f'*** Seconds since start: {seconds_since_start} ({locked_state}), Variance {variance:.2f}')
        params = self.tracking_params

        # Generate Doppler-shifted and phase-shifted carrier wave
        # Adjust the time domain based on our current time
        time_domain = self.time_domain_for_1ms + seconds_since_start
        #time_domain = self.time_domain_for_1ms
        doppler_shift_carrier = np.exp(
            -1j * ((2 * np.pi * params.current_doppler_shift * time_domain) + params.current_carrier_wave_phase_shift)
        )
        if False:
            import matplotlib.pyplot as plt
            plt.cla()
            plt.plot(doppler_shift_carrier)
            plt.show()
            plt.pause(0.1)
        doppler_shifted_samples = samples * doppler_shift_carrier

        # Correlate early, prompt, and late phase versions of the PRN
        unslid_prn = params.satellite.prn_as_complex
        prompt_prn = np.roll(unslid_prn, params.current_prn_code_phase_shift)   # type: ignore

        coherent_prompt_correlation = frequency_domain_correlation(doppler_shifted_samples, prompt_prn)
        non_coherent_prompt_correlation = np.abs(coherent_prompt_correlation)
        non_coherent_prompt_peak_offset = np.argmax(non_coherent_prompt_correlation)

        # Recenter the code phase offset so that it looks positive or negative, depending on where the offset sits
        # in the period of the PRN.
        if non_coherent_prompt_peak_offset <= SAMPLES_PER_PRN_TRANSMISSION / 2:
            centered_non_coherent_prompt_peak_offset = non_coherent_prompt_peak_offset
        else:
            centered_non_coherent_prompt_peak_offset = non_coherent_prompt_peak_offset - SAMPLES_PER_PRN_TRANSMISSION

        if centered_non_coherent_prompt_peak_offset > 0:
            params.current_prn_code_phase_shift += 1
        elif centered_non_coherent_prompt_peak_offset < 0:
            params.current_prn_code_phase_shift -= 1

        # Finally, ensure we're always sliding within one PRN transmission
        params.current_prn_code_phase_shift = int(params.current_prn_code_phase_shift) % SAMPLES_PER_PRN_TRANSMISSION

        coherent_prompt_prn_correlation_peak = coherent_prompt_correlation[non_coherent_prompt_peak_offset]
        navigation_bit_pseudosymbol_value = int(np.sign(coherent_prompt_prn_correlation_peak.real))
        params.navigation_bit_pseudosymbols.append(navigation_bit_pseudosymbol_value)

        # Questions:
        # Amplitude of the PRN vs. amplitude of the input signal
        # If I'm running an LPF on the carrier loop discriminator, what would be the cutoff?
        # If I'm doing the error on the whole set of samples at once, how would I integrate the error?

        samples_with_prn_wiped_off = samples * prompt_prn
        #self._test(seconds_since_start, samples_with_prn_wiped_off)
        #self._follow_carrier_via_correlation_profile(coherent_prompt_correlation, coherent_prompt_prn_correlation_peak)
        # self._run_tracking_loop(seconds_since_start, samples_with_prn_wiped_off)
        #self._run_tracking_loop2(seconds_since_start, samples_with_prn_wiped_off)
        #self._adjust_tracking_parameters_via_correlation_profile(coherent_prompt_prn_correlation_peak)
        #self._adjust_tracking_parameters_via_correlation_profile_all(coherent_prompt_correlation)
        #self._run_tracking_loop(seconds_since_start, samples * prompt_prn)
        x = doppler_shifted_samples * prompt_prn
        self._is.append(coherent_prompt_prn_correlation_peak.real)
        self._qs.append(coherent_prompt_prn_correlation_peak.imag)
        #self.iq_angles.append(np.angle(coherent_prompt_prn_correlation_peak))
        #self._is.append(x[non_coherent_prompt_peak_offset].real)
        #self._qs.append(x[non_coherent_prompt_peak_offset].imag)
        #self._test2(doppler_shifted_samples * prompt_prn, non_coherent_prompt_peak_offset)
        #self._test2(coherent_prompt_correlation, non_coherent_prompt_peak_offset)
        self._test5(coherent_prompt_correlation, coherent_prompt_prn_correlation_peak)
        #self._test6(seconds_since_start, samples_with_prn_wiped_off)
        self.carrier_phases.append(params.current_carrier_wave_phase_shift)

        # logging.info(f"Doppler shift {params.current_doppler_shift:.2f}, Carrier phase {params.current_carrier_wave_phase_shift:.8f}")
        # logging.info(f"Code phase {params.current_prn_code_phase_shift}")

        params.doppler_shifts.append(params.current_doppler_shift)
        params.carrier_wave_phases.append(params.current_carrier_wave_phase_shift)
        #params.carrier_wave_phase_errors.append(carrier_wave_phase_error)

        start = 1699037280
        failures = 1699037347
        #          1699037383
        #          1699037347
        #          1699037377
        #          1699037389
        #          1699037455 (1.0, 0.2, had some unknown bits but recovered, only stopped because we flipped polarity)
        #          1699037437 (0.1, 0.01)
        #          1699037449 (still going!)
        #          1699037497 (wow! 0.3, 0.1)
        #          1699037515 (0.5, 0.2, only stopped because we flipped polarity)
        #          1699037515 (0.5, 0.5, only stopped because we flipped polarity)
        #          1699037515 (0.5, 1, only stopped because we flipped polarity)
        duration_till_failures = failures - start

        #print(f'Doppler {self.tracking_params.current_doppler_shift} Phase {self.tracking_params.current_carrier_wave_phase_shift}')

        if True:
            import matplotlib.pyplot as plt
            if seconds_since_start % 5 >= 4.99:
                self.constellation_ax.clear()
                self.phase_errors_ax.clear()
                self.tracking_params.carrier_wave_phase_errors = []

            #if (seconds_since_start % 1) * 1000 >= 996:
            if (seconds_since_start % 1) == 0:
                #plt.cla()
                self.freq_ax.plot(params.doppler_shifts[::10])

                #coords = list(zip(self._is, self._qs))
                points = list(complex(i, q) for i, q in zip(self._is, self._qs))
                #lowest = min([x.real for x in points])
                #highest = max([x.real for x in points])
                #midpoint = ((highest - lowest) / 2.0) + lowest
                points_on_left_pole = [p for p in points if p.real < 0]
                points_on_right_pole = [p for p in points if p.real >= 0]
                #left_point = np.mean([x.real for x in points_on_left_pole])
                #right_point = np.mean([x.real for x in points_on_right_pole])
                left_point = np.mean(points_on_left_pole)
                right_point = np.mean(points_on_right_pole)
                #print(f'({left_point.imag:.2f}, {right_point.imag:.2f})')
                angle = 180 - (((np.arctan2(left_point.imag, left_point.real) / math.tau) * 360) % 180)
                print(f'Angle: {angle:.2f}')
                # Don't look 'below' the axis (TODO(PT): Clean all this up)
                self.constellation_ax.scatter(self._is, self._qs)
                self.constellation_ax.scatter([left_point.real, right_point.real], [left_point.imag, right_point.imag])
                self.i_ax.clear()
                self.i_ax.plot(self._is)
                self._is = []

                self.q_ax.clear()
                self.q_ax.plot(self._qs)
                self._qs = []

                self.iq_angle_ax.clear()
                self.iq_angle_ax.plot(self.iq_angles)
                self.iq_angles = []

                self.carrier_phase_ax.clear()
                self.carrier_phase_ax.plot(self.carrier_phases)
                self.carrier_phases = []

                #self.samples_ax.plot(samples_with_prn_wiped_off)
                #filtered = butter_lowpass_filter(samples_with_prn_wiped_off, params.current_doppler_shift * 1.1, SAMPLES_PER_SECOND, 2)
                #self.samples_ax.plot(self.filtered)
                #self.filtered = []
                #doppler_shift_carrier = np.exp(-1j * ((2 * np.pi * params.current_doppler_shift * time_domain) + params.current_carrier_wave_phase_shift)) * 0.005
                #doppler_shifted_multiplier = np.exp(-1j * ((2 * np.pi * params.current_doppler_shift * time_domain) + params.current_carrier_wave_phase_shift)) * 0.005
                #self.samples_ax.plot(doppler_shifted_multiplier)
                self.carrier = []
                #self.samples_ax.plot(self.mixed)
                self.mixed = []

                #self.phase_errors_ax.plot(self.phase_errors[::10])
                self.phase_errors_ax.plot(self.tracking_params.carrier_wave_phase_errors)

            # "The output of an FFT is an array of complex numbers, and each complex number gives you the magnitude and phase, and the index of that number gives you the frequency."
                # We're already taking the FFT from the correlation profile, maybe we *can* just read the info from that!

                plt.pause(0.001)
            #if seconds_since_start % 5 ==0:
            #    self.constellation_fig.clear()

        if False and seconds_since_start >= duration_till_failures:
            import matplotlib.pyplot as plt
            print(f'shwoing extras...')
            self.errors_ax.plot(params.carrier_wave_phase_errors[::2046])
            self.phase_ax.plot(params.carrier_wave_phases[::2046])
            plt.pause(0.0000000000001)

        return NavigationBitPseudosymbol.from_val(navigation_bit_pseudosymbol_value)

# TODO(PT): Is the Costas loop meant to track the phase wave-by-wave, or is that meant to be taken care of by the `time_domain` offset?!
"The carrier phase measurement is actually a measurement of the beat frequency between the received carrier of the satellite signal and a receiver-generated reference frequency."
# Could we just do a 'correlation profile' with the carrier wave?! Maybe no because we don't know the exact frequency...
# DO THE SAME kind of correlation where we correlate with the conjugate of the carrier wave & look at the offset of the peak?!?!?!?!
# https://gnss-sdr.org/docs/sp-blocks/tracking/ this shows the *same* correlation result being used for the phase discriminator?!?!?!
