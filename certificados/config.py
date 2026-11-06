import getpass
import os
import re
import secrets

from werkzeug.security import generate_password_hash


PASSWORD_METHOD = "scrypt:32768:8:3"


def secret_from_env(name):
    value = os.environ.get(name, "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise RuntimeError(f"Configure {name} com 64 caracteres hexadecimais.")
    return bytes.fromhex(value)


def main():
    password = getpass.getpass("Senha administrativa (mínimo 14 caracteres): ")
    if not 14 <= len(password) <= 128:
        raise SystemExit("Escolha uma senha com 14 a 128 caracteres.")
    if password != getpass.getpass("Confirme a senha administrativa: "):
        raise SystemExit("As senhas digitadas são diferentes.")
    print(f"PROSPER_SESSION_SECRET={secrets.token_hex(32)}")
    print(f"PROSPER_CPF_KEY={secrets.token_hex(32)}")
    print(f"PROSPER_ADMIN_PASSWORD_HASH={generate_password_hash(password, method=PASSWORD_METHOD)}")


if __name__ == "__main__":
    main()
