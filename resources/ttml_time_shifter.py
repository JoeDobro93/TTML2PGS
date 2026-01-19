import re
import sys

# CONFIGURATION
# ---------------------------------------------------------
INPUT_FILE = 'Parasite (2020).+9395ms2match.ja.ttml'  # Replace with your file name
OUTPUT_FILE = 'Parasite_shifted.ja.ttml'
SHIFT_MS = 9395  # How many milliseconds to add (can be negative)
TICK_RATE = 10000000  # 10 million ticks per second


# ---------------------------------------------------------

def shift_timestamps():
    # Calculate how many ticks to add
    # 1 ms = (Tick Rate / 1000) ticks
    # For 10,000,000 rate, 1 ms = 10,000 ticks
    ticks_per_ms = TICK_RATE // 1000
    tick_offset = int(SHIFT_MS * ticks_per_ms)

    try:
        with open(INPUT_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: Could not find file '{INPUT_FILE}'")
        return

    # Regex Explanation:
    # (?P<prefix>(?:begin|end)=["'])  -> Match 'begin="' or 'end="' and capture it (group 1)
    # (?P<ticks>\d+)                  -> Match the digits (group 2)
    # (?P<suffix>t["'])               -> Match the 't' and closing quote (group 3)
    pattern = re.compile(r'(?P<prefix>(?:begin|end)=["\'])(?P<ticks>\d+)(?P<suffix>t["\'])')

    def replacement_func(match):
        original_ticks = int(match.group('ticks'))
        new_ticks = original_ticks + tick_offset

        # Ensure we don't end up with negative time
        if new_ticks < 0:
            new_ticks = 0

        return f"{match.group('prefix')}{new_ticks}{match.group('suffix')}"

    # Perform the substitution
    new_content = pattern.sub(replacement_func, content)

    # Write the result
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(new_content)

    print(f"Successfully created '{OUTPUT_FILE}' with a {SHIFT_MS}ms shift.")


if __name__ == "__main__":
    shift_timestamps()