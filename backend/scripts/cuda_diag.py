"""Add the cu13/bin/x86_64 layout to the DLL path and verify CUDA EP loads."""
import os, glob, ctypes
import onnxruntime as ort

site = os.path.dirname(os.path.dirname(ort.__file__))  # site-packages
nvidia = os.path.join(site, "nvidia")

# CUDA 13 unified wheels: DLLs live under nvidia/<comp>/bin AND nvidia/cu13/bin/x86_64
dirs = []
for pat in ["cu13/bin/x86_64", "cudnn/bin", "*/bin/x86_64", "*/bin"]:
    dirs += glob.glob(os.path.join(nvidia, pat))
dirs = [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]
for d in dirs:
    n = len(glob.glob(os.path.join(d, "*.dll")))
    print(f"add_dll_directory: {d.replace(site,'...')}  ({n} dlls)")
    os.add_dll_directory(d)
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

cuda_dll = os.path.join(os.path.dirname(ort.__file__), "capi", "onnxruntime_providers_cuda.dll")
print("\nloading provider dll...")
try:
    ctypes.WinDLL(cuda_dll)
    print("RESULT: onnxruntime_providers_cuda.dll loaded OK")
except OSError as e:
    print("RESULT: FAILED ->", str(e)[:120])

print("\n=== probe key DLLs ===")
for name in ["cudart64_13.dll", "cublas64_13.dll", "cublasLt64_13.dll",
             "cudnn64_9.dll", "nvrtc64_130_0.dll", "cufft64_12.dll", "curand64_10.dll"]:
    try:
        ctypes.WinDLL(name); print(f"  OK   {name}")
    except OSError as e:
        print(f"  MISS {name}")
