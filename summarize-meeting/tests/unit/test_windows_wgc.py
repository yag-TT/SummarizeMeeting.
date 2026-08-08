from winrt.windows.storage.streams import Buffer

from summarize_meeting.capture.screen.windows_wgc import _bgra_buffer_to_bgr


def test_bgra_buffer_to_bgr_drops_alpha_and_owns_result() -> None:
    source = Buffer(8)
    source.length = 8
    memoryview(source)[:] = bytes([10, 20, 30, 40, 50, 60, 70, 80])

    result = _bgra_buffer_to_bgr(source, width=2, height=1)

    assert result.tolist() == [[[10, 20, 30], [50, 60, 70]]]
    memoryview(source)[0] = 99
    assert result[0, 0, 0] == 10
