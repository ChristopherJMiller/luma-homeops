"""Q-BRIDGE PortList bitmaps: port N is bit 0x80 >> (N-1)%8 of byte (N-1)//8."""


def decode(hexstr: str) -> set[int]:
    data = bytes.fromhex(hexstr.replace(" ", ""))
    return {i * 8 + b + 1 for i, byte in enumerate(data) for b in range(8) if byte & (0x80 >> b)}


def encode(ports: set[int], length: int) -> str:
    data = bytearray(length)
    for p in ports:
        if not 1 <= p <= length * 8:
            raise ValueError(f"port {p} does not fit a {length}-byte PortList")
        data[(p - 1) // 8] |= 0x80 >> ((p - 1) % 8)
    return " ".join(f"{b:02X}" for b in data)
