import os


def test_print_fee_info_cmd():
    exit_code = os.system("chia show -f")
    assert exit_code == 0
