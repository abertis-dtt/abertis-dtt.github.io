#!/usr/bin/env python3
import os
import time
import urllib.request
import subprocess

# =======================================================================
# CONFIGURATION
# =======================================================================
# Define your transponders, their feed service reference, and their PIDs.
# 'sref' is the Enigma2 Service Reference of the feed channel on that frequency.
TRANSPONDERS = {
    "11222_H_30000": {
        "sref": "1:0:1:BD1:EA74:1:CE40000:0:0:0:",
        "pids": [2025, 2026, 2027, 2028, 2035, 2050, 2060]
    },
    "11347_H_20858": {
        "sref": "1:0:1:CE5:EA77:0:CE40000:0:0:0:",
        "pids": [2302, 2303, 2305, 2306, 2308]
    },
    "11382_H_30000": {
        "sref": "1:0:1:DC0:EA79:1:CE40000:0:0:0:",
        "pids": [2520, 2521, 2522, 2523, 2524, 2531, 2532, 2533, 2534]
    },
    "11653_H_19680": {
        "sref": "1:0:1:515:EA63:0:CE40000:0:0:0:",
        "pids": [301, 303]
    },
    "11674_H_15750": {
        "sref": "1:0:1:58C:EA64:0:CE40000:0:0:0:",
        "pids": [420, 421, 423, 431, 433]
    },
    "12548_V_29600": {
        "sref": "1:0:1:6A5:EA67:0:CE40000:0:0:0:",
        "pids": [701, 702, 703]
    },
    "12671_V_30000": {
        "sref": "1:0:1:3E8:EA61:0:CE40000:0:0:0:",
        "pids": [8000, 8004, 8005, 8006]
    },
    "12631_V_30000": {
        "sref": "1:0:1:3E9:EA62:0:CE40000:0:0:0:",
        "pids": [8001, 8002, 8003]
    },
    "12584_V_16900": {
        "sref": "1:0:1:3F0:EA68:0:CE43128:0:0:0:",
        "pids": [8008]
    }
}

# The command used to analyze the stream. 
# Because astra.conf outputs the streams to a local HTTP port, we analyze that URL.
ANALYZE_CMD_TEMPLATE = "astra --analyze http://127.0.0.1:9999/abertis/pid{pid}"

# How long (in seconds) to run the analysis for each PID to gather SDT/PMT data
ANALYZE_DURATION = 22

# Output directory for the logs
OUTPUT_DIR = "pid_logs"

# =======================================================================

def zap_to_service(sref):
    """Zap to the specified Enigma2 service reference using the local Web Interface."""
    # Encode colons to ensure WebIf properly parses the string
    encoded_sref = sref.replace(':', '%3A')
    url = f"http://127.0.0.1/web/zap?sRef={encoded_sref}"
    try:
        print(f"[*] Zapping to feed service: {sref}")
        # Call the local WebIf to change the channel
        with urllib.request.urlopen(url, timeout=5) as response:
            result = response.read().decode('utf-8')
            if "true" in result.lower():
                print("    -> Zap successful. Waiting 6 seconds for tuner lock & stream start...")
                time.sleep(6) # Give the tuner time to lock and Astra time to decrypt/route
                return True
            else:
                print("    -> Zap failed. Check the sRef format.")
                return False
    except Exception as e:
        print(f"    -> Error during zap (Is WebIf running?): {e}")
        return False

def analyze_pid(pid):
    """Run astra --analyze for the specified PID, wait, then terminate and save log."""
    log_filepath = os.path.join(OUTPUT_DIR, f"pid{pid}.log")
    cmd = ANALYZE_CMD_TEMPLATE.format(pid=pid)
    
    print(f"[*] Analyzing PID {pid} for {ANALYZE_DURATION} seconds...")
    
    with open(log_filepath, "w", encoding='utf-8') as log_file:
        try:
            # Start the astra analysis process in the background
            process = subprocess.Popen(
                cmd, 
                shell=True, 
                stdout=log_file, 
                stderr=subprocess.STDOUT
            )
            
            # astra --analyze runs forever, so we intentionally wait for a timeout
            process.wait(timeout=ANALYZE_DURATION)
            
        except subprocess.TimeoutExpired:
            # Time is up! Terminate the process to finish the log
            process.terminate()
            process.wait()
            print(f"    -> Finished PID {pid}. Log saved to {log_filepath}")
        except Exception as e:
            print(f"    -> Error running analysis for PID {pid}: {e}")

def main():
    # 1. Create the pid_logs directory if it doesn't exist
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"Created directory: {OUTPUT_DIR}/")

    total_pids_scanned = 0

    # 2. Iterate through each Transponder group
    for tp_name, tp_data in TRANSPONDERS.items():
        print("\n" + "="*50)
        print(f"Starting Scan for Transponder/Group: {tp_name}")
        print("="*50)
        
        sref = tp_data.get("sref")
        pids = tp_data.get("pids", [])
        
        if not sref or not pids:
            print(f"Skipping {tp_name}: Missing sref or pids configuration.")
            continue

        # 3. Zap to the transponder's feed channel
        zap_success = zap_to_service(sref)
        
        if not zap_success:
            print(f"Skipping PIDs for {tp_name} due to zap failure.")
            continue

        # 4. Scan each PID belonging to this frequency
        for pid in pids:
            analyze_pid(pid)
            total_pids_scanned += 1

    print("\n" + "="*50)
    print(f"Scan Complete! Processed {total_pids_scanned} PIDs.")
    print(f"All logs are safely stored in the '{OUTPUT_DIR}' directory.")
    print("="*50)

if __name__ == "__main__":
    main()