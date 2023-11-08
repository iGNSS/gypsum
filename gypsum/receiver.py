import collections
import logging
from enum import Enum
from enum import auto
from typing import Callable
from typing import Type

import numpy as np

from gypsum.acquisition import GpsSatelliteDetector
from gypsum.acquisition import SatelliteAcquisitionAttemptResult
from gypsum.events import UnknownEventError
from gypsum.gps_ca_prn_codes import GpsSatelliteId
from gypsum.gps_ca_prn_codes import generate_replica_prn_signals
from gypsum.constants import SAMPLES_PER_PRN_TRANSMISSION
from gypsum.navigation_bit_intergrator import CannotDetermineBitPhaseEvent
from gypsum.navigation_bit_intergrator import DeterminedBitPhaseEvent
from gypsum.navigation_bit_intergrator import EmitNavigationBitEvent
from gypsum.navigation_bit_intergrator import Event
from gypsum.navigation_bit_intergrator import NavigationBitIntegrator
from gypsum.navigation_message_decoder import NavigationMessageDecoder
from gypsum.satellite import GpsSatellite
from gypsum.antenna_sample_provider import AntennaSampleProvider
from gypsum.config import ACQUISITION_INTEGRATION_PERIOD_MS
from gypsum.tracker import GpsSatelliteTracker
from gypsum.tracker import GpsSatelliteTrackingParameters
from gypsum.utils import AntennaSamplesSpanningOneMs
from gypsum.utils import chunks
from gypsum.utils import does_list_contain_sublist


_logger = logging.getLogger(__name__)


class TrackingState(Enum):
    PROVISIONAL_PROBE = auto()
    LOCKED = auto()


class GpsSatelliteSignalProcessingPipeline:
    satellite: GpsSatellite
    state: TrackingState

    # Tracks PRN code phase shift, carrier wave Doppler shift, and carrier wave phase
    tracker: GpsSatelliteTracker

    pseudosymbol_integrator: NavigationBitIntegrator
    navigation_message_decoder: NavigationMessageDecoder

    def __init__(self, satellite: GpsSatellite, acquisition_result: SatelliteAcquisitionAttemptResult) -> None:
        self.satellite = satellite
        self.state = TrackingState.PROVISIONAL_PROBE
        tracking_params = GpsSatelliteTrackingParameters(
            satellite=satellite,
            current_doppler_shift=acquisition_result.doppler_shift,
            current_carrier_wave_phase_shift=acquisition_result.carrier_wave_phase_shift,
            current_prn_code_phase_shift=acquisition_result.prn_phase_shift,
            doppler_shifts=[],
            carrier_wave_phases=[],
            carrier_wave_phase_errors=[],
            navigation_bit_pseudosymbols=[],
        )
        self.tracker = GpsSatelliteTracker(tracking_params)
        self.pseudosymbol_integrator = NavigationBitIntegrator()
        self.navigation_message_decoder = NavigationMessageDecoder()

    def process_samples(self, samples: AntennaSamplesSpanningOneMs, sample_index: int):
        pseudosymbol = self.tracker.process_samples(samples, sample_index)
        integrator_events = self.pseudosymbol_integrator.process_pseudosymbol(pseudosymbol)

        integrator_event_type_to_callback: dict[Type[Event], Callable[[Event], None]] = {   # type: ignore
            DeterminedBitPhaseEvent: self._handle_integrator_determined_bit_phase,
            CannotDetermineBitPhaseEvent: self._handle_integrator_cannot_determine_bit_phase,
            EmitNavigationBitEvent: self._handle_integrator_emitted_bit,
        }
        for event in integrator_events:
            event_type = type(event)
            if event_type not in integrator_event_type_to_callback:
                raise UnknownEventError(event_type)
            callback = integrator_event_type_to_callback[event_type]
            callback(event)

    def _handle_integrator_determined_bit_phase(self, event: DeterminedBitPhaseEvent) -> None:
        satellite_id = self.satellite.satellite_id.id
        _logger.info(
            f'Integrator for SV({satellite_id}) has determined bit phase {event.bit_phase}'
        )

    def _handle_integrator_cannot_determine_bit_phase(self, event: CannotDetermineBitPhaseEvent) -> None:
        satellite_id = self.satellite.satellite_id.id
        _logger.info(
            f'Integrator for SV({satellite_id} could not determine bit phase. Confidence: {int(event.confidence*100)}%'
        )
        # TODO(PT): Untrack this satellite (as the bits are low confidence)
        raise NotImplementedError(f'Satellite should be removed from the tracking pool')

    def _handle_integrator_emitted_bit(self, event: EmitNavigationBitEvent) -> None:
        satellite_id = self.satellite.satellite_id.id
        _logger.info(
            f'Integrator for SV({satellite_id}) emitted bit {event.bit_value}'
        )
        decoder_events = self.navigation_message_decoder.process_bit_from_satellite(event.bit_value)
        for event in decoder_events:
            print(event)


class GpsReceiver:
    def __init__(self, antenna_samples_provider: AntennaSampleProvider) -> None:
        self.antenna_samples_provider = antenna_samples_provider

        # Generate the replica signals that we'll use to correlate against the received antenna signals upfront
        satellites_to_replica_prn_signals = generate_replica_prn_signals()
        self.satellites_by_id = {
            satellite_id: GpsSatellite(satellite_id=satellite_id, prn_code=code)
            for satellite_id, code in satellites_to_replica_prn_signals.items()
        }
        # TODO(PT): Perhaps this state should belong to the detector.
        # The receiver can remove satellites from the pool when it decides a satellite has been acquired
        #self.satellite_ids_eligible_for_acquisition = deepcopy(ALL_SATELLITE_IDS)
        self.satellite_ids_eligible_for_acquisition = [GpsSatelliteId(id=32)]
        self.satellite_detector = GpsSatelliteDetector(self.satellites_by_id)
        # Used during acquisition to integrate correlation over a longer period than a millisecond.
        self.rolling_samples_buffer = collections.deque(maxlen=ACQUISITION_INTEGRATION_PERIOD_MS)

        self.tracked_satellite_ids_to_processing_pipelines: dict[GpsSatelliteId, GpsSatelliteSignalProcessingPipeline] = {}

    def step(self):
        """Run one 'iteration' of the GPS receiver. This consumes one millisecond of antenna data."""
        sample_index = self.antenna_samples_provider.cursor
        samples: AntennaSamplesSpanningOneMs = self.antenna_samples_provider.get_samples(SAMPLES_PER_PRN_TRANSMISSION)
        # Firstly, record this sample in our rolling buffer
        self.rolling_samples_buffer.append(samples)

        # If we need to perform acquisition, do so now
        #if len(self.tracked_satellite_ids_to_processing_pipelines) < MINIMUM_TRACKED_SATELLITES_FOR_POSITION_FIX:
        if len(self.tracked_satellite_ids_to_processing_pipelines) < 1:
            _logger.info(
                f"Will perform acquisition search because we're only "
                f"tracking {len(self.tracked_satellite_ids_to_processing_pipelines)} satellites"
            )
            self._perform_acquisition()

        # Continue tracking each acquired satellite
        self._track_acquired_satellites(samples, sample_index)

    def decode_nav_bits(self, sat: GpsSatelliteTrackingParameters):
        navigation_bit_pseudosymbols = sat.navigation_bit_pseudosymbols
        confidence_scores = []
        for roll in range(0, 20):
            phase_shifted_bits = navigation_bit_pseudosymbols[roll:]
            confidences = []
            for twenty_pseudosymbols in chunks(phase_shifted_bits, 20):
                integrated_value = sum(twenty_pseudosymbols)
                confidences.append(abs(integrated_value))
            # Compute an overall confidence score for this offset
            confidence_scores.append(np.mean(confidences))

        # print(f"Confidence scores: {confidence_scores}")
        best_offset = np.argmax(confidence_scores)
        print(f"Best Offset: {best_offset} ({confidence_scores[best_offset]})")

        bit_phase = best_offset
        phase_shifted_bits = navigation_bit_pseudosymbols[bit_phase:]
        bits = []
        for twenty_pseudosymbols in chunks(phase_shifted_bits, 20):
            integrated_value = sum(twenty_pseudosymbols)
            bit_value = np.sign(integrated_value)
            bits.append(bit_value)

        digital_bits = [1 if b == 1.0 else 0 for b in bits]
        inverted_bits = [0 if b == 1.0 else 1 for b in bits]
        print(f"Bit count: {len(digital_bits)}")
        print(f"Bits:          {digital_bits}")
        print(f"Inverted bits: {inverted_bits}")

        preamble = [1, 0, 0, 0, 1, 0, 1, 1]
        inverted = [0, 1, 1, 1, 0, 1, 0, 0]
        print(f"Preamble {preamble} found in bits? {does_list_contain_sublist(digital_bits, preamble)}")
        print(f"Preamble {preamble} found in inverted bits? {does_list_contain_sublist(inverted_bits, preamble)}")

        def get_matches(l, sub):
            return [l[pos : pos + len(sub)] == sub for pos in range(0, len(l) - len(sub) + 1)]

        preamble_starts_in_digital_bits = [
            x[0] for x in (np.argwhere(np.array(get_matches(digital_bits, preamble)) == True))
        ]
        print(f"Preamble starts in bits:          {preamble_starts_in_digital_bits}")
        preamble_starts_in_inverted_bits = [
            x[0] for x in (np.argwhere(np.array(get_matches(inverted_bits, preamble)) == True))
        ]
        print(f"Preamble starts in inverted bits: {preamble_starts_in_inverted_bits}")

    def _perform_acquisition(self) -> None:
        # To improve signal-to-noise ratio during acquisition, we integrate antenna data over 20ms.
        # Therefore, we keep a rolling buffer of the last few samples.
        # If this buffer isn't primed yet, we can't do any work yet.
        if len(self.rolling_samples_buffer) < ACQUISITION_INTEGRATION_PERIOD_MS:
            _logger.info(f"Skipping acquisition attempt because the history buffer isn't primed yet.")
            return

        _logger.info(
            f"Performing acquisition search over {len(self.satellite_ids_eligible_for_acquisition)} satellites."
        )

        samples_for_integration_period = np.concatenate(self.rolling_samples_buffer)
        newly_acquired_satellites = self.satellite_detector.detect_satellites_in_antenna_data(
            self.satellite_ids_eligible_for_acquisition,
            samples_for_integration_period,
        )
        for satellite_acquisition_result in newly_acquired_satellites:
            sat_id = satellite_acquisition_result.satellite_id
            satellite = self.satellites_by_id[sat_id]
            self.tracked_satellite_ids_to_processing_pipelines[sat_id] = GpsSatelliteSignalProcessingPipeline(satellite, satellite_acquisition_result)

    def _track_acquired_satellites(self, samples: AntennaSamplesSpanningOneMs, sample_index: int):
        for _satellite_id, pipeline in self.tracked_satellite_ids_to_processing_pipelines.items():
            pipeline.process_samples(samples, sample_index)