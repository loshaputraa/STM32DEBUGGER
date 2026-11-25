import os
import cv2
import numpy as np
import time  # Added for camera wake-up delays

# ---- configuration ----
MAX_CAMERA_INDEX = 3  # checks indices 0 to 3
W, H = 640, 480  # must match aruco.py
CHESSBOARD_SIZE = (8, 5)  # INNER corners (squares minus 1 in each direction)
SQUARE_SIZE = 0.025  # metres (aruco.py expects metres)
MIN_FRAMES = 15
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calib_data.npz")


def open_camera(index):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def choose_camera():
    """Show each camera in turn. Y = use it, N = next camera, Q = quit."""
    for index in range(MAX_CAMERA_INDEX):
        print(f"Trying camera index {index}...")
        cap = open_camera(index)

        # Give the DirectShow driver a moment to initialize the USB stream
        time.sleep(0.5)

        # Retry reading a frame up to 10 times (1 second total)
        ok = False
        for _ in range(10):
            ok, _ = cap.read()
            if ok:
                break
            time.sleep(0.1)

        if not ok:
            cap.release()
            print(f"Index {index}: No camera found or failed to wake up.")
            continue

        print(f"Index {index}: Camera found. Y = use this camera, N = next, Q = quit")

        # Create a named window to prevent it from hiding behind other windows
        cv2.namedWindow("Select camera", cv2.WINDOW_NORMAL)

        while True:
            ok, frame = cap.read()
            if not ok:
                print(f"Index {index}: Lost connection.")
                break

            view = frame.copy()
            cv2.putText(view, f"camera {index}  (Y = use, N = next, Q = quit)", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.imshow("Select camera", view)

            key = cv2.waitKey(1) & 0xFF

            if key == ord('y'):
                cap.release()
                cv2.destroyAllWindows()
                cv2.waitKey(1)  # Crucial: lets the OS process the window destruction
                return index

            if key == ord('n'):
                break  # Breaks the while loop to go to the next index

            if key == ord('q'):
                cap.release()
                cv2.destroyAllWindows()
                cv2.waitKey(1)
                raise SystemExit("Cancelled.")

        # Clean up before trying the next index
        cap.release()
        cv2.destroyAllWindows()
        cv2.waitKey(1)  # Crucial: lets the OS process the window destruction
        time.sleep(0.5)  # Wait a moment before opening the next camera

    raise SystemExit("No camera found or selected.")


# ---- step 1: pick the camera ----
camera_index = choose_camera()
print(f"\nUsing camera {camera_index}. Set the ArUco tab's Camera box to {camera_index}.\n")

# ---- chessboard coordinates (in metres) ----
objp = np.zeros((CHESSBOARD_SIZE[0] * CHESSBOARD_SIZE[1], 3), np.float32)
objp[:, :2] = np.mgrid[0:CHESSBOARD_SIZE[0], 0:CHESSBOARD_SIZE[1]].T.reshape(-1, 2)
objp *= SQUARE_SIZE

criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
objpoints = []
imgpoints = []

# ---- step 2: open the chosen camera in the ArUco mode ----
cap = open_camera(camera_index)
ok, test = cap.read()
if not ok:
    raise SystemExit("Could not read a frame from the camera.")
h, w = test.shape[:2]
if (w, h) != (W, H):
    raise SystemExit(f"Camera gave {w}x{h}, but calibration must be {W}x{H} to match aruco.py.")

print("Point the camera at the chessboard.")
print("Press 'c' to capture a frame (try for 15-20 frames from different angles and distances).")
print("Press 'q' to quit and calculate the calibration.")

# ---- step 3: capture frames ----
while True:
    ok, frame = cap.read()
    if not ok:
        continue

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    found, corners = cv2.findChessboardCorners(gray, CHESSBOARD_SIZE, None)

    display = frame.copy()
    if found:
        cv2.drawChessboardCorners(display, CHESSBOARD_SIZE, corners, found)

    cv2.putText(display, f"Captured: {len(objpoints)}/{MIN_FRAMES}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cv2.imshow("Camera Calibration", display)

    key = cv2.waitKey(1) & 0xFF
    if key == ord('c') and found:
        corners2 = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
        objpoints.append(objp)
        imgpoints.append(corners2)
        print(f"Frame {len(objpoints)} captured")
    elif key == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()

# ---- step 4: calculate and save ----
if len(objpoints) < MIN_FRAMES:
    raise SystemExit(f"Only {len(objpoints)} frames captured, need at least {MIN_FRAMES}. Nothing saved.")

print("\nCalculating calibration...")
rms, mtx, dist, _, _ = cv2.calibrateCamera(objpoints, imgpoints, (W, H), None, None)

print("\n--- Camera matrix ---")
print(mtx)
print(f"\nRMS reprojection error: {rms:.3f} px (under about 0.5 is good)")

np.savez(OUT_PATH, mtx=mtx, dist=dist)
print(f"Saved: {OUT_PATH}")
print(f"Now set the ArUco tab's Camera box to {camera_index}.")