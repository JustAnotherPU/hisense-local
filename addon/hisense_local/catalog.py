"""Names and value tables. Data only.

Which controls exist is decided by the frames in the active session, not here:
a value without a recorded frame is never offered.
"""

PROGRAMS = {  # Selected_program_ID, the 18 positions of the dial
    9: "Eco 40-60",
    7: "Cotton",
    8: "Synthetics",
    42: "Auto",
    57: "Full load",
    52: "Power 30'",
    11: "Quick 15'",
    4: "Refresh",
    16: "Baby care",
    15: "Allergy care",
    56: "Pet hair",
    44: "Bedding",
    19: "Duvet",
    10: "Wool",
    18: "Sportswear",
    20: "Rinse + spin",
    6: "Drum clean",
    14: "Spin only",
}

# temperature=1 is accepted by the washer but changes nothing: left out on purpose.
TEMPERATURES = {
    0: "Cold",
    2: "20 °C",
    3: "30 °C",
    4: "40 °C",
    6: "60 °C",
    9: "90 °C",
}

SPINS = {v: f"{v * 100} rpm" for v in range(4, 15, 2)}
SPINS[0] = "No spin"

DELAYS = {h: f"{h} h" for h in range(1, 25)}
DELAYS[0] = "None"

SELECTS = {  # property -> (entity key, value names)
    "Selected_program_ID": ("program", PROGRAMS),
    "temperature": ("temperature", TEMPERATURES),
    "Spin_speed_rpm": ("spin", SPINS),
    "DelayStartTime": ("delay", DELAYS),
}

BUTTONS = {  # entity key -> (property, value)
    "start": ("Actions", 2),
    "pause": ("Remote_hold", 1),
    "resume": ("Remote_hold", 0),
    "stop": ("Actions", 1),
}

SWITCH_NAMES = {  # any other property recorded with both 0 and 1 becomes a switch
    "ColdWash": "Cold wash",
    "Extra_Rinse": "Extra rinse",
    "EcoMode": "Eco mode",
    "Steam": "Steam",
    "Prewash": "Prewash",
    "IntensiveWash": "Intensive wash",
    "AntiCrease": "Anti-crease",
    "Gentle_Dry": "Gentle dry",
    "QuickerMode": "Quicker",
    "Quiet_model": "Quiet",
    "Half_Load": "Half load",
    "Stain_removal": "Stain removal",
    "Time_Save": "Time save",
    "DoseAid": "Dose aid",
    "UV_Light": "UV light",
    "Child_lock": "Child lock",
    "DownLight": "Drum light",
}

# Recorded but not exposed in v1: dose amounts, water level, soil level, spin_time.
NOT_EXPOSED = {"Dose_Detergent_amount", "Dose_Softener_amount", "WaterLevel", "Soil_Lever", "spin_time"}

ENTITY_NAMES = {
    "program": "Program",
    "temperature": "Temperature",
    "spin": "Spin",
    "delay": "Delay start",
    "start": "Start",
    "pause": "Pause",
    "resume": "Resume",
    "stop": "Stop",
    "last_result": "Last command",
    "frames": "Recorded frames",
    "session": "Session",
    "mode": "Mode",
}

# Sensors read from the status block. Offsets are filled by calibration
# (selfcheck --calibrate writes /data/status_offsets.json); none is assumed here.
SENSOR_NAMES = {  # field -> (name, unit)
    "minutes_left": ("Time left", "min"),
    "machine_status": ("State", None),
    "phase": ("Phase", None),
    "door": ("Door", None),
    "error_code": ("Error code", None),
}
STATUS_OFFSETS = {}

# Known durations (minutes) used by calibration to find the minutes-left byte.
KNOWN_DURATIONS = {9: 239, 11: 15, 14: 12, 4: 19, 10: 55}

# Calibration steps: settings only, nothing that starts the drum.
CALIBRATION_PROPS = ["Selected_program_ID", "temperature", "Spin_speed_rpm", "DelayStartTime",
                     "Child_lock", "DownLight"] + [p for p in SWITCH_NAMES if p not in ("Child_lock", "DownLight")]
QUICK_PROGRAM = 11
DEFAULT_PROGRAM = 9


# Status readings already settled on a tested model, used when no calibration has run.
# A calibration file, when present, always wins.
PROFILES = {
    "WF5I1045BWQ": {
        "Selected_program_ID": {"at": 18, "map": {"26": 42, "27": 44, "33": 52, "37": 56, "38": 57}},
        "temperature": 19,
        "Spin_speed_rpm": 20,
        "Child_lock": [22, 8, 8],
        "Prewash": [22, 16, 16],
        "AntiCrease": [22, 64, 64],
        "DownLight": [69, 8, 8],
        "minutes_left": 29,
        "machine_status": {"at": 92, "map": {"127": "ready", "64": "running", "74": "stopped"}},
    },
}
