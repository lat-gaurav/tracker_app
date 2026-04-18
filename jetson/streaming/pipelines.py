"""GStreamer pipeline strings: camera capture and RTSP factory launch."""
from jetson.constants import STREAM_W, STREAM_H, FPS


def build_cam_pipeline(device, camera_cfg):
    """Return a GStreamer pipeline string for a V4L2 camera.

    If camera_cfg.width/height/fps are set (non-zero), request that exact
    sensor mode.  Otherwise auto-negotiate.  Either way, output is scaled
    to STREAM_W × STREAM_H @ FPS for the encoder.
    """
    cw = int(camera_cfg.get("width", 0))
    ch = int(camera_cfg.get("height", 0))
    cf = int(camera_cfg.get("fps", 0))

    if cw > 0 and ch > 0 and cf > 0:
        src_caps = f'video/x-raw,width={cw},height={ch},framerate={cf}/1 ! '
        print(f"[CAP]   Camera caps: {cw}x{ch}@{cf}")
    else:
        src_caps = ''
        print(f"[CAP]   Camera caps: auto-negotiate")

    return (
        f'v4l2src device={device} ! '
        f'{src_caps}'
        'videoconvert ! videoscale ! '
        f'video/x-raw,format=BGR,width={STREAM_W},height={STREAM_H} ! '
        f'videorate ! video/x-raw,framerate={FPS}/1 ! '
        'appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false'
    )


def build_rtsp_launch():
    """Pipeline string for the RTSP server's media factory.

    appsrc (BGR) -> videoconvert I420 -> videoflip (rotation) -> nvvidconv NVMM
    -> nvv4l2h264enc -> rtph264pay.
    """
    return (
        f'( appsrc name=src is-live=true format=time block=false max-buffers=1 leaky-type=2 '
        f'caps=video/x-raw,format=BGR,width={STREAM_W},height={STREAM_H},framerate={FPS}/1 ! '
        'videoconvert ! video/x-raw,format=I420 ! '
        'videoflip name=vflip method=0 ! '
        'nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! '
        'nvv4l2h264enc bitrate=1500000 iframeinterval=30 preset-level=1 insert-sps-pps=1 ! '
        'h264parse config-interval=1 ! '
        'rtph264pay name=pay0 pt=96 aggregate-mode=zero-latency )'
    )
