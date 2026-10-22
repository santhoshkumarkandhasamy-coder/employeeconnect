"""Small bundled deny-list (lower-case). Extend with a larger list if desired."""

COMMON_PASSWORDS = frozenset(
    """
password passw0rd p@ssword p@ssw0rd 123456 12345678 123456789 1234567890 qwerty qwertyuiop qwerty123 abc123
letmein welcome welcome1 admin admin123 administrator iloveyou monkey dragon football baseball master login
hello hello123 sunshine princess shadow superman trustno1 whatever freedom starwars michael jennifer
changeme change123 default secret test test123 testing guest root toor user temp temp123 summer winter spring
autumn india india123 bangalore chennai mumbai delhi company office employee employee123 welcome123 abcd1234
asdfgh asdfghjkl zxcvbnm zxcvbn qazwsx 1q2w3e4r 1qaz2wsx pass1234 passcode google facebook cricket
""".split()
)
