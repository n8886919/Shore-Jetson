"""Reject system-console ports without opening or configuring a UART."""
from pathlib import Path


def require_control_port(device: str, *, proc: Path = Path('/proc'),
                         sys: Path = Path('/sys')) -> None:
    resolved = Path(device).resolve()
    name = resolved.name
    if name.startswith(('ttyTCU', 'ttyUTC')):
        raise ValueError('Combined debug UART cannot control Servo; use the 40-pin UART or USB-TTL')
    active = sys / 'class/tty/console/active'
    if active.exists() and name in active.read_text().split():
        raise ValueError('UART is an active system console')
    cmdline = proc / 'cmdline'
    if cmdline.exists():
        consoles = [word.split('=', 1)[1].split(',')[0]
                    for word in cmdline.read_text().split() if word.startswith('console=')]
        if name in consoles or str(resolved) in consoles:
            raise ValueError('UART is reserved by the kernel console')
    driver = sys / 'class/tty' / name / 'device/driver'
    if driver.is_symlink() and 'tcu' in driver.resolve().name.lower():
        raise ValueError('Combined debug UART driver is not a control UART')
