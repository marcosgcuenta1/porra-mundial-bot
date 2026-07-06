# -*- coding: utf-8 -*-
"""
Datos estáticos del bot: equipos (nombre en español, bandera y alias para
casar con los nombres que devuelve la API) y la porra de Marcos.

Fuente de la porra: porra-marcos-gracia-arrondo.pdf (carpeta personal).
1 = gana local · 2 = gana visitante · X = empate.
"""

# code -> (nombre_display, bandera, [alias normalizados para casar con la API])
# Los alias se comparan en minúsculas, sin acentos ni espacios, contra el
# nombre y el TLA (abreviatura de 3 letras) que devuelve football-data.org.
TEAMS = {
    "MEX": ("México",          "🇲🇽", ["mex", "mexico"]),
    "RSA": ("Sudáfrica",       "🇿🇦", ["rsa", "sudafrica", "southafrica"]),
    "KOR": ("Corea del Sur",   "🇰🇷", ["kor", "coreadelsur", "southkorea", "korearepublic", "korea"]),
    "CZE": ("Chequia",         "🇨🇿", ["cze", "chequia", "czechia", "czechrepublic"]),

    "CAN": ("Canadá",          "🇨🇦", ["can", "canada"]),
    "BIH": ("Bosnia",          "🇧🇦", ["bih", "bosnia", "bosniaandherzegovina", "bosniaherzegovina"]),
    "QAT": ("Qatar",           "🇶🇦", ["qat", "qatar"]),
    "SUI": ("Suiza",           "🇨🇭", ["sui", "suiza", "switzerland"]),

    "BRA": ("Brasil",          "🇧🇷", ["bra", "brasil", "brazil"]),
    "MAR": ("Marruecos",       "🇲🇦", ["mar", "marruecos", "morocco"]),
    "HAI": ("Haití",           "🇭🇹", ["hai", "haiti"]),
    "SCO": ("Escocia",         "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f", ["sco", "escocia", "scotland"]),

    "USA": ("EE.UU.",          "🇺🇸", ["usa", "eeuu", "unitedstates", "unitedstatesofamerica"]),
    "PAR": ("Paraguay",        "🇵🇾", ["par", "paraguay"]),
    "AUS": ("Australia",       "🇦🇺", ["aus", "australia"]),
    "TUR": ("Turquía",         "🇹🇷", ["tur", "turquia", "turkey", "turkiye"]),

    "GER": ("Alemania",        "🇩🇪", ["ger", "alemania", "germany"]),
    "CUW": ("Curazao",         "🇨🇼", ["cuw", "curazao", "curacao"]),
    "CIV": ("C.Marfil",        "🇨🇮", ["civ", "cmarfil", "costademarfil", "cotedivoire", "ivorycoast"]),
    "ECU": ("Ecuador",         "🇪🇨", ["ecu", "ecuador"]),

    "NED": ("Holanda",         "🇳🇱", ["ned", "hol", "holanda", "netherlands", "paisesbajos"]),
    "JPN": ("Japón",           "🇯🇵", ["jpn", "japon", "japan"]),
    "SWE": ("Suecia",          "🇸🇪", ["swe", "suecia", "sweden"]),
    "TUN": ("Túnez",           "🇹🇳", ["tun", "tunez", "tunisia", "tunecia"]),

    "BEL": ("Bélgica",         "🇧🇪", ["bel", "belgica", "belgium"]),
    "EGY": ("Egipto",          "🇪🇬", ["egy", "egipto", "egypt"]),
    "IRN": ("Irán",            "🇮🇷", ["irn", "iran", "iranislamicrepublic"]),
    "NZL": ("N.Zelanda",       "🇳🇿", ["nzl", "nzelanda", "nuevazelanda", "newzealand"]),

    "ESP": ("España",          "🇪🇸", ["esp", "espana", "spain"]),
    "CPV": ("Cabo Verde",      "🇨🇻", ["cpv", "caboverde", "capeverde", "capeverdeislands"]),
    "KSA": ("Arabia Saudita",  "🇸🇦", ["ksa", "sau", "arabiasaudita", "saudiarabia", "arabiasaudi"]),
    "URU": ("Uruguay",         "🇺🇾", ["uru", "uruguay"]),

    "FRA": ("Francia",         "🇫🇷", ["fra", "francia", "france"]),
    "SEN": ("Senegal",         "🇸🇳", ["sen", "senegal"]),
    "IRQ": ("Irak",            "🇮🇶", ["irq", "irak", "iraq"]),
    "NOR": ("Noruega",         "🇳🇴", ["nor", "noruega", "norway"]),

    "ARG": ("Argentina",       "🇦🇷", ["arg", "argentina"]),
    "ALG": ("Argelia",         "🇩🇿", ["alg", "dza", "argelia", "algeria"]),
    "AUT": ("Austria",         "🇦🇹", ["aut", "austria"]),
    "JOR": ("Jordania",        "🇯🇴", ["jor", "jordania", "jordan"]),

    "POR": ("Portugal",        "🇵🇹", ["por", "prt", "portugal"]),
    "COD": ("Congo",           "🇨🇩", ["cod", "congo", "drcongo", "congodr", "democraticrepublicofcongo", "rdcongo"]),
    "UZB": ("Uzbekistán",      "🇺🇿", ["uzb", "uzbekistan"]),
    "COL": ("Colombia",        "🇨🇴", ["col", "colombia"]),

    "ENG": ("Inglaterra",      "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f", ["eng", "inglaterra", "england"]),
    "CRO": ("Croacia",         "🇭🇷", ["cro", "croacia", "croatia"]),
    "GHA": ("Ghana",           "🇬🇭", ["gha", "ghana"]),
    "PAN": ("Panamá",          "🇵🇦", ["pan", "panama"]),
}

# La porra, en el mismo orden que el PDF. (local, visitante, pronóstico)
# pronóstico: "1" gana local · "2" gana visitante · "X" empate
PORRA = [
    # Grupo A
    ("MEX", "RSA", "1"), ("KOR", "CZE", "1"), ("CZE", "RSA", "1"),
    ("MEX", "KOR", "1"), ("CZE", "MEX", "2"), ("RSA", "KOR", "2"),
    # Grupo B
    ("CAN", "BIH", "1"), ("QAT", "SUI", "2"), ("SUI", "BIH", "1"),
    ("CAN", "QAT", "1"), ("SUI", "CAN", "X"), ("BIH", "QAT", "2"),
    # Grupo C
    ("BRA", "MAR", "1"), ("HAI", "SCO", "2"), ("BRA", "HAI", "1"),
    ("SCO", "MAR", "2"), ("SCO", "BRA", "2"), ("MAR", "HAI", "1"),
    # Grupo D
    ("USA", "PAR", "1"), ("AUS", "TUR", "2"), ("TUR", "PAR", "1"),
    ("USA", "AUS", "1"), ("TUR", "USA", "2"), ("PAR", "AUS", "1"),
    # Grupo E
    ("GER", "CUW", "1"), ("CIV", "ECU", "2"), ("GER", "CIV", "1"),
    ("ECU", "CUW", "1"), ("ECU", "GER", "2"), ("CUW", "CIV", "2"),
    # Grupo F
    ("NED", "JPN", "1"), ("SWE", "TUN", "1"), ("NED", "SWE", "1"),
    ("TUN", "JPN", "2"), ("TUN", "NED", "2"), ("JPN", "SWE", "X"),
    # Grupo G
    ("BEL", "EGY", "1"), ("IRN", "NZL", "1"), ("BEL", "IRN", "1"),
    ("NZL", "EGY", "2"), ("NZL", "BEL", "2"), ("EGY", "IRN", "1"),
    # Grupo H
    ("ESP", "CPV", "1"), ("KSA", "URU", "2"), ("ESP", "KSA", "1"),
    ("URU", "CPV", "1"), ("URU", "ESP", "2"), ("CPV", "KSA", "2"),
    # Grupo I
    ("FRA", "SEN", "1"), ("IRQ", "NOR", "2"), ("FRA", "IRQ", "1"),
    ("NOR", "SEN", "2"), ("NOR", "FRA", "2"), ("SEN", "IRQ", "1"),
    # Grupo J
    ("ARG", "ALG", "1"), ("AUT", "JOR", "1"), ("ARG", "AUT", "1"),
    ("JOR", "ALG", "2"), ("JOR", "ARG", "2"), ("ALG", "AUT", "2"),
    # Grupo K
    ("POR", "COD", "1"), ("UZB", "COL", "2"), ("POR", "UZB", "1"),
    ("COL", "COD", "1"), ("COL", "POR", "2"), ("COD", "UZB", "1"),
    # Grupo L
    ("ENG", "CRO", "1"), ("GHA", "PAN", "2"), ("ENG", "GHA", "1"),
    ("PAN", "CRO", "2"), ("PAN", "ENG", "2"), ("CRO", "GHA", "1"),
]
