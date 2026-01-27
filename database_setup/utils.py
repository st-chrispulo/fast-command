import secrets
import string


def generate_password(length: int = 16) -> str:
    safe_symbols = "-._~!#$^*"
    chars = string.ascii_letters + string.digits + safe_symbols
    return ''.join(secrets.choice(chars) for _ in range(length))


if __name__ == "__main__":
    print(generate_password(24))
