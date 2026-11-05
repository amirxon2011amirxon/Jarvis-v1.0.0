"""Audio-device cache behaviour, without opening a real device.

The full picker needs PortAudio hardware; these tests only exercise the cache
and probe-reset contracts that a RESCAN DEVICES action depends on.
"""

from __future__ import annotations

import unittest
from unittest import mock

from core import audio_devices


class CacheResetTests(unittest.TestCase):
    def test_configure_invalidates_probe_results_and_chosen_apis(self):
        with mock.patch.object(audio_devices, "_cache", {"input": ["old"]}), \
             mock.patch.dict(
                 audio_devices._probe_results,
                 {("mme", "input"): False}, clear=True), \
             mock.patch.dict(
                 audio_devices._chosen_api,
                 {"input": "mme", "output": "mme"}, clear=True):
            audio_devices.configure(16000, 24000)

            self.assertIsNone(audio_devices._cache)
            self.assertEqual(audio_devices._probe_results, {})
            self.assertEqual(audio_devices._chosen_api,
                             {"input": None, "output": None})

    def test_rescan_clears_negative_transport_results(self):
        fresh = {"input": ["New mic"], "output": ["New speaker"]}
        with mock.patch.object(
                audio_devices, "_cache",
                {"input": ["Old mic"], "output": ["Old speaker"]}), \
             mock.patch.dict(
                 audio_devices._probe_results,
                 {("mme", "input"): False}, clear=True), \
             mock.patch.dict(
                 audio_devices._chosen_api,
                 {"input": "mme", "output": "mme"}, clear=True), \
             mock.patch.object(
                 audio_devices, "_query", return_value=fresh) as query:
            devices = audio_devices.list_devices("input", refresh=True)

        self.assertEqual(devices, ["New mic"])
        query.assert_called_once_with()
        self.assertEqual(audio_devices._probe_results, {})


class DefaultVolumeTests(unittest.TestCase):
    def setUp(self):
        endpoint = mock.Mock()
        endpoint.GetMasterVolumeLevelScalar.return_value = 0.42
        self.endpoint = endpoint
        self.speakers = type("Speakers", (), {"EndpointVolume": endpoint})()

    def test_the_system_default_endpoint_has_a_volume(self):
        with mock.patch.object(audio_devices, "_platform_is",
                               return_value="Windows"), \
             mock.patch(
                 "pycaw.pycaw.AudioUtilities.GetSpeakers",
                 return_value=self.speakers):
            self.assertEqual(audio_devices.get_output_volume(""), 42)
            self.assertEqual(
                audio_devices.get_output_volume(
                    audio_devices.DEFAULT_LABEL), 42)

    def test_the_system_default_volume_can_be_set(self):
        with mock.patch.object(audio_devices, "_platform_is",
                               return_value="Windows"), \
             mock.patch(
                 "pycaw.pycaw.AudioUtilities.GetSpeakers",
                 return_value=self.speakers):
            self.assertTrue(audio_devices.set_output_volume(25, ""))
        self.endpoint.SetMasterVolumeLevelScalar.assert_called_once_with(
            0.25, None)


if __name__ == "__main__":
    unittest.main()
