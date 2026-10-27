"""Log in to Garmin Connect once, on your own computer, and print the tokens.

Paste the printed JSON into the GARMIN_TOKENS repository secret. The daily job
starts from those tokens and keeps them refreshed, so it never needs your
password or an MFA code.

    pip install garminconnect==0.3.20
    python scripts/garmin_login.py
"""

import getpass

from garminconnect import Garmin

email = input("Garmin email: ")
password = getpass.getpass("Garmin password: ")
client = Garmin(email, password, prompt_mfa=lambda: input("MFA code: "))
client.login()
print("\nCopy everything on the next line into the GARMIN_TOKENS secret:\n")
print(client.client.dumps())
