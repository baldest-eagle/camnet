# CamNet receiver package
#
# Platform support:
#   - Windows: DirectShow virtual camera via Win32 Named Shared Memory
#   - Linux:   V4L2 loopback device + POSIX SHM mirror
#
# Use `platform_shm.create_frame_writer()` for platform-agnostic frame output.
