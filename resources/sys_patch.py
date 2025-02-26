# Framework for mounting and patching macOS root volume
# Copyright (C) 2020-2022, Dhinak G, Mykola Grymalyuk
# Missing Features:
# - Full System/Library Snapshotting (need to research how Apple achieves this)
#   - Temporary Work-around: sudo bless --mount /System/Volumes/Update/mnt1 --bootefi --last-sealed-snapshot
# - Work-around battery throttling on laptops with no battery (IOPlatformPluginFamily.kext/Contents/PlugIns/ACPI_SMC_PlatformPlugin.kext/Contents/Resources/)

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
import sys

from resources import constants, device_probe, utilities, generate_smbios
from data import sip_data, sys_patch_data, model_array, os_data, smbios_data, cpu_data, dylib_data


class PatchSysVolume:
    def __init__(self, model, versions):
        self.model = model
        self.constants: constants.Constants() = versions
        self.computer = self.constants.computer
        self.root_mount_path = None
        self.sip_enabled = True
        self.sbm_enabled = True
        self.amfi_enabled = True
        self.fv_enabled = True
        self.dosdude_patched = True
        self.nvidia_legacy = False
        self.kepler_gpu = False
        self.amd_ts1 = False
        self.amd_ts2 = False
        self.iron_gpu = False
        self.sandy_gpu = False
        self.ivy_gpu = False
        self.brightness_legacy = False
        self.legacy_audio = False
        self.legacy_wifi = False
        self.legacy_gmux = False
        self.legacy_keyboard_backlight = False
        self.added_legacy_kexts = False
        self.amfi_must_disable = False
        self.check_board_id = False
        self.bad_board_id = False
        self.no_patch = True
        self.validate = False
        self.supports_metal = False

        if self.constants.detected_os > os_data.os_data.catalina:
            # Big Sur and newer use APFS snapshots
            self.mount_location = "/System/Volumes/Update/mnt1"
            self.mount_location_data = ""
        else:
            self.mount_location = ""
            self.mount_location_data = ""
        self.mount_coreservices = f"{self.mount_location}/System/Library/CoreServices"
        self.mount_extensions = f"{self.mount_location}/System/Library/Extensions"
        self.mount_frameworks = f"{self.mount_location}/System/Library/Frameworks"
        self.mount_lauchd = f"{self.mount_location}/System/Library/LaunchDaemons"
        self.mount_private_frameworks = f"{self.mount_location}/System/Library/PrivateFrameworks"
        self.mount_libexec = f"{self.mount_location}/usr/libexec"
        self.mount_extensions_mux = f"{self.mount_location}/System/Library/Extensions/AppleGraphicsControl.kext/Contents/PlugIns/"
        self.mount_private_etc = f"{self.mount_location_data}/private/etc"
        self.mount_application_support = f"{self.mount_location_data}/Library/Application Support"

    def find_mount_root_vol(self, patch):
        self.root_mount_path = utilities.get_disk_path()
        if self.root_mount_path.startswith("disk"):
            if self.constants.detected_os == os_data.os_data.catalina and self.validate is False:
                print("- Mounting Catalina Root Volume as writable")
                utilities.elevated(["mount", "-uw", f"{self.mount_location}/"], stdout=subprocess.PIPE).stdout.decode().strip().encode()
            print(f"- Found Root Volume at: {self.root_mount_path}")
            if Path(self.mount_extensions).exists():
                print("- Root Volume is already mounted")
                if patch is True:
                    # Root Volume unpatching is unreliable due to being a live volume
                    # Only worth while on Big Sur as '--last-sealed-snapshot' is hit or miss
                    if self.constants.detected_os == os_data.os_data.big_sur and utilities.check_seal() is True:
                        self.backup_volume()
                    self.patch_root_vol()
                    return True
                else:
                    self.unpatch_root_vol()
                    return True
            else:
                if self.constants.detected_os > os_data.os_data.catalina:
                    print("- Mounting APFS Snapshot as writable")
                    result = utilities.elevated(["mount", "-o", "nobrowse", "-t", "apfs", f"/dev/{self.root_mount_path}", self.mount_location], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    if result.returncode == 0:
                        print(f"- Mounted APFS Snapshot as writable at: {self.mount_location}")
                if Path(self.mount_extensions).exists():
                    print("- Successfully mounted the Root Volume")
                    if patch is True:
                        # Root Volume unpatching is unreliable due to being a live volume
                        # Only worth while on Big Sur as '--last-sealed-snapshot' is hit or miss
                        if self.constants.detected_os == os_data.os_data.big_sur and utilities.check_seal() is True:
                            self.backup_volume()
                        self.patch_root_vol()
                        return True
                    else:
                        self.unpatch_root_vol()
                        return True
                else:
                    print("- Failed to mount the Root Volume")
                    print("- Recommend rebooting the machine and trying to patch again")
                    if self.constants.gui_mode is False:
                        input("- Press [ENTER] to exit: ")
        else:
            print("- Could not find root volume")
            if self.constants.gui_mode is False:
                input("- Press [ENTER] to exit: ")

    def backup_volume(self):
        for location in sys_patch_data.BackupLocations:
            utilities.cls()
            print("Backing up root volume before patching (This may take some time)")
            print(f"- Attempting to backup {location}")
            location_zip = f"{location}-Backup.zip"
            location_zip_path = Path(self.mount_location) / Path(location_zip)

            if location_zip_path.exists():
                print(f"- Found existing {location_zip}, skipping")
            else:
                print(f"- Backing up {location}")
                # cp -r ./Extensions ./Extensions-Backup
                # ditto -c -k --sequesterRsrc --keepParent ./Extensions-Backup ./Extensions-Backup.zip
                # rm -r ./Extensions-Backup

                print("- Creating Backup folder")
                utilities.process_status(
                    utilities.elevated(
                        ["cp", "-r", f"{self.mount_location}/{location}", f"{self.mount_location}/{location}-Backup"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                )
                print("- Zipping Backup folder")
                utilities.process_status(
                    utilities.elevated(
                        ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", f"{self.mount_location}/{location}-Backup", f"{self.mount_location}/{location_zip}"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                )

                print("- Removing Backup folder")
                utilities.process_status(
                    utilities.elevated(
                        ["rm", "-r", f"{self.mount_location}/{location}-Backup"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                )

    def manual_root_patch_revert(self):
        print("- Attempting to revert patches")
        if (Path(self.mount_location) / Path("/System/Library/Extensions-Backup.zip")).exists():
            print("- Verified manual unpatching is available")

            for location in sys_patch_data.BackupLocations:
                utilities.cls()
                print("Reverting root volume patches (This may take some time)")

                print(f"- Attempting to unpatch {location}")
                location_zip = f"/{location}-Backup.zip"
                location_zip_path = Path(self.mount_location) / Path(location_zip)
                location_old_path = Path(self.mount_location) / Path(location)

                if "PrivateFrameworks" in location:
                    copy_path = Path(self.mount_location) / Path("/System/Library/PrivateFrameworks")
                elif "Frameworks" in location:
                    copy_path = Path(self.mount_location) / Path("/System/Library/Frameworks")
                else:
                    copy_path = Path(self.mount_location) / Path("/System/Library")

                if location_zip_path.exists():
                    print(f"- Found {location_zip}")

                    print(f"- Unzipping {location_zip}")
                    utilities.process_status(utilities.elevated(["unzip", location_zip_path, "-d", copy_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

                    if location_old_path.exists():
                        print(f"- Renaming {location}")
                        utilities.process_status(utilities.elevated(["mv", location_old_path, f"{location_old_path}-Patched"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

                    print(f"- Renaming {location}-Backup")
                    utilities.process_status(utilities.elevated(["mv", f"{location_old_path}-Backup", location_old_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

                    print(f"- Removing {location_old_path}-Patched")
                    utilities.process_status(utilities.elevated(["rm", "-r", f"{location_old_path}-Patched"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

                    # ditto will create a '__MACOSX' folder
                    # print("- Removing __MACOSX folder")
                    # utilities.process_status(utilities.elevated(["rm", "-r", f"{copy_path}/__MACOSX"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

                else:
                    print(f"- Failed to find {location_zip}, unable to unpatch")
            if self.validate is False:
                self.rebuild_snapshot()
        else:
            print("- Could not find Extensions.zip, cannot manually unpatch root volume")

    def unpatch_root_vol(self):
        if self.constants.detected_os > os_data.os_data.catalina:
            print("- Reverting to last signed APFS snapshot")
            result = utilities.elevated(["bless", "--mount", self.mount_location, "--bootefi", "--last-sealed-snapshot"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if result.returncode != 0:
                print("- Unable to revert root volume patches")
                print("Reason for unpatch Failure:")
                print(result.stdout.decode())
                print("- Failed to revert snapshot via bless, falling back on manual restoration")
                self.manual_root_patch_revert()
            else:
                print("- Unpatching complete")
                print("\nPlease reboot the machine for patches to take effect")
        else:
            self.manual_root_patch_revert()

    def rebuild_snapshot(self):
        print("- Rebuilding Kernel Cache (This may take some time)")
        if self.constants.detected_os > os_data.os_data.catalina:
            result = utilities.elevated(["kmutil", "install", "--volume-root", self.mount_location, "--update-all"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        else:
            result = utilities.elevated(["kextcache", "-i", f"{self.mount_location}/"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        # kextcache always returns 0, even if it fails
        # Check the output for 'KernelCache ID' to see if the cache was successfully rebuilt
        if result.returncode != 0 or (self.constants.detected_os < os_data.os_data.catalina and "KernelCache ID" not in result.stdout.decode()):
            self.success_status = False
            print("- Unable to build new kernel cache")
            print("\nPlease report this to Github")
            print("Reason for Patch Failure:")
            print(result.stdout.decode())
            print("")
            print("\nPlease reboot the machine to avoid potential issues rerunning the patcher")
            if self.constants.gui_mode is False:
                input("Press [ENTER] to continue")
        else:
            self.success_status = True
            print("- Successfully built new kernel cache")
            # if self.constants.gui_mode is False:
            #     if self.constants.detected_os > os_data.os_data.catalina:
            #         input("Press [ENTER] to continue with snapshotting")
            #     else:
            #         input("Press [ENTER] to continue with kernel and dyld cache merging")
            if self.constants.detected_os > os_data.os_data.catalina:
                print("- Creating new APFS snapshot")
                bless = utilities.elevated(
                    ["bless", "--folder", f"{self.mount_location}/System/Library/CoreServices", "--bootefi", "--create-snapshot"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
                )
                if bless.returncode != 0:
                    print("- Unable to create new snapshot")
                    print("Reason for snapshot failure:")
                    print(bless.stdout.decode())
                    if "Can't use last-sealed-snapshot or create-snapshot on non system volume" in bless.stdout.decode():
                        print("- This is an APFS bug with Monterey! Perform a clean installation to ensure your APFS volume is built correctly")
                    return
                else:
                    self.unmount_drive()
            else:
                if self.constants.detected_os == os_data.os_data.catalina:
                    print("- Merging kernel cache")
                    utilities.process_status(utilities.elevated(["kcditto"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
                print("- Merging dyld cache")
                utilities.process_status(utilities.elevated(["update_dyld_shared_cache", "-root", f"{self.mount_location}/"]))
            print("- Patching complete")
            print("\nPlease reboot the machine for patches to take effect")
            if self.amd_ts2 is True and self.constants.allow_ts2_accel is True:
                print(
                    """\nPlease note that with ATI TeraScale 2 GPUs, you may experience colour strobing
on reboot. Please use SwitchResX or ResXtreme to force 1 million colours on your
monitor to fix this. If you are epileptic, please ask for someone to aid you or
set million colour before rebooting"""
                )
            if self.constants.gui_mode is False:
                input("\nPress [ENTER] to continue")

    def unmount_drive(self):
        print("- Unmounting Root Volume (Don't worry if this fails)")
        utilities.elevated(["diskutil", "unmount", self.root_mount_path], stdout=subprocess.PIPE).stdout.decode().strip().encode()

    def delete_old_binaries(self, vendor_patch):
        for delete_current_kext in vendor_patch:
            delete_path = Path(self.mount_extensions) / Path(delete_current_kext)
            if Path(delete_path).exists():
                print(f"- Deleting {delete_current_kext}")
                utilities.process_status(utilities.elevated(["rm", "-R", delete_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
            else:
                print(f"- Couldn't find {delete_current_kext}, skipping")

    def add_new_binaries(self, vendor_patch, vendor_location):
        for add_current_kext in vendor_patch:
            existing_path = Path(self.mount_extensions) / Path(add_current_kext)
            if Path(existing_path).exists():
                print(f"- Found conflicting kext, Deleting Root Volume's {add_current_kext}")
                utilities.process_status(utilities.elevated(["rm", "-R", existing_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
            print(f"- Adding {add_current_kext}")
            utilities.process_status(utilities.elevated(["cp", "-R", f"{vendor_location}/{add_current_kext}", self.mount_extensions], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
            utilities.process_status(utilities.elevated(["chmod", "-Rf", "755", f"{self.mount_extensions}/{add_current_kext}"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
            utilities.process_status(utilities.elevated(["chown", "-Rf", "root:wheel", f"{self.mount_extensions}/{add_current_kext}"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        
    def add_brightness_patch(self):
        self.delete_old_binaries(sys_patch_data.DeleteBrightness)
        self.add_new_binaries(sys_patch_data.AddBrightness, self.constants.legacy_brightness)
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_private_frameworks_path_brightness}/", self.mount_private_frameworks], stdout=subprocess.PIPE)
        utilities.process_status(utilities.elevated(["chmod", "-Rf", "755", f"{self.mount_private_frameworks}/DisplayServices.framework"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        utilities.process_status(utilities.elevated(["chown", "-Rf", "root:wheel", f"{self.mount_private_frameworks}/DisplayServices.framework"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

    def add_audio_patch(self):
        if self.model in ["iMac7,1", "iMac8,1"]:
            self.delete_old_binaries(sys_patch_data.DeleteVolumeControl)
            self.add_new_binaries(sys_patch_data.AddVolumeControl, self.constants.audio_path)
        else:
            self.add_new_binaries(sys_patch_data.AddVolumeControlv2, self.constants.audio_v2_path)

    def add_wifi_patch(self):
        print("- Merging Wireless CoreSerices patches")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.legacy_wifi_coreservices}/", self.mount_coreservices], stdout=subprocess.PIPE)
        utilities.process_status(utilities.elevated(["chmod", "-Rf", "755", f"{self.mount_coreservices}/WiFiAgent.app"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        utilities.process_status(utilities.elevated(["chown", "-Rf", "root:wheel", f"{self.mount_coreservices}/WiFiAgent.app"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        print("- Merging Wireless usr/libexec patches")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.legacy_wifi_libexec}/", self.mount_libexec], stdout=subprocess.PIPE)
        utilities.process_status(utilities.elevated(["chmod", "755", f"{self.mount_libexec}/airportd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        utilities.process_status(utilities.elevated(["chown", "root:wheel", f"{self.mount_libexec}/airportd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

        # dylib patch to resolve password crash prompt
        # Note requires ASentientBot's SkyLight to function
        # Thus Metal machines do not benefit from this patch, however install anyways as harmless 
        print("- Merging Wireless SkyLightPlugins")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.legacy_wifi_support}/", self.mount_application_support], stdout=subprocess.PIPE)

    def add_legacy_mux_patch(self):
        self.delete_old_binaries(sys_patch_data.DeleteDemux)
        print("- Merging Legacy Mux Kext patches")
        utilities.process_status(
            utilities.elevated(["cp", "-R", f"{self.constants.legacy_mux_path}/AppleMuxControl.kext", self.mount_extensions_mux], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        )
    
    def add_legacy_keyboard_backlight_patch(self):
        print("- Merging Keyboard Backlight SkyLightPlugins")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.legacy_keyboard_backlight_support}/", self.mount_application_support], stdout=subprocess.PIPE)
    
    def add_legacy_dropbox_patch(self):
        print("- Merging DropboxHack SkyLightPlugins")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.legacy_dropbox_support}/", self.mount_application_support], stdout=subprocess.PIPE)

    def gpu_accel_legacy(self):
        if self.constants.detected_os == os_data.os_data.mojave:
            print("- Installing General Acceleration Kext patches for Mojave")
            self.add_new_binaries(sys_patch_data.AddGeneralAccelMojave, self.constants.legacy_general_path)
        elif self.constants.detected_os == os_data.os_data.catalina:
            print("- Installing General Acceleration Kext patches for Catalina")
            self.add_new_binaries(sys_patch_data.AddGeneralAccelCatalina, self.constants.legacy_general_path)
        elif self.constants.detected_os in [os_data.os_data.big_sur, os_data.os_data.monterey]:
            print("- Installing General Acceleration Kext patches for Big Sur/Monterey")
            self.add_new_binaries(sys_patch_data.AddGeneralAccel, self.constants.legacy_general_path)

    # Nvidia
    def gpu_accel_legacy_nvidia_master(self):
        if self.constants.detected_os in [os_data.os_data.mojave, os_data.os_data.catalina]:
            print("- Installing Nvidia Acceleration Kext patches for Mojave/Catalina")
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddNvidiaAccelLegacy, self.constants.legacy_nvidia_path)
        elif self.constants.detected_os in [os_data.os_data.big_sur, os_data.os_data.monterey]:
            print("- Installing Nvidia Acceleration Kext patches for Big Sur/Monterey")
            self.delete_old_binaries(sys_patch_data.DeleteNvidiaAccel11)
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddNvidiaAccel11, self.constants.legacy_nvidia_path)
            if self.constants.detected_os == os_data.os_data.monterey and self.constants.detected_os_minor > 0:
                # Beta 7+ removes NVDAStartup
                self.add_new_binaries(sys_patch_data.AddNvidiaTeslaAccel12, self.constants.legacy_nvidia_kepler_path)
        else:
            print("- Installing basic Nvidia Framebuffer Kext patches for generic OS")
            self.add_new_binaries(sys_patch_data.AddNvidiaBrightness, self.constants.legacy_nvidia_path)

    # AMD/ATI
    def gpu_accel_legacy_ts1_master(self):
        if self.constants.detected_os in [os_data.os_data.mojave, os_data.os_data.catalina]:
            print("- Installing TeraScale 1 Acceleration Kext patches for Mojave/Catalina")
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddAMDAccelLegacy, self.constants.legacy_amd_path)
        elif self.constants.detected_os in [os_data.os_data.big_sur, os_data.os_data.monterey]:
            print("- Installing TeraScale 1 Acceleration Kext patches for Big Sur/Monterey")
            self.delete_old_binaries(sys_patch_data.DeleteAMDAccel11)
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddAMDAccel11, self.constants.legacy_amd_path)
        else:
            print("- Installing basic TeraScale 1 Framebuffer Kext patches for generic OS")
            self.add_new_binaries(sys_patch_data.AddAMDBrightness, self.constants.legacy_amd_path)

    def gpu_accel_legacy_ts2_master(self):
        if self.constants.detected_os in [os_data.os_data.mojave, os_data.os_data.catalina] and self.constants.allow_ts2_accel is True:
            print("- Installing TeraScale 2 Acceleration Kext patches for Mojave/Catalina")
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddAMDAccelLegacy, self.constants.legacy_amd_path)
        elif self.constants.detected_os in [os_data.os_data.big_sur, os_data.os_data.monterey] and self.constants.allow_ts2_accel is True:
            # TODO: Enable for Monterey when acceleration patches proress
            print("- Installing TeraScale 2 Acceleration Kext patches for Big Sur")
            self.delete_old_binaries(sys_patch_data.DeleteAMDAccel11)
            self.delete_old_binaries(sys_patch_data.DeleteAMDAccel11TS2)
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddAMDAccel11, self.constants.legacy_amd_path)
        else:
            print("- Installing basic TeraScale 2 Framebuffer Kext patches for generic OS")
            self.add_new_binaries(sys_patch_data.AddAMDBrightness, self.constants.legacy_amd_path)

    # Intel
    def gpu_accel_legacy_ironlake_master(self):
        if self.constants.detected_os in [os_data.os_data.mojave, os_data.os_data.catalina]:
            print("- Installing Ironlake Acceleration Kext patches for Mojave/Catalina")
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddIntelGen1Accel, self.constants.legacy_intel_gen1_path)
        elif self.constants.detected_os in [os_data.os_data.big_sur, os_data.os_data.monterey]:
            print("- Installing Ironlake Acceleration Kext patches for Big Sur/Monterey")
            self.delete_old_binaries(sys_patch_data.DeleteNvidiaAccel11)
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddIntelGen1Accel, self.constants.legacy_intel_gen1_path)
        else:
            print("- Installing basic Ironlake Framebuffer Kext patches for generic OS")
            self.add_new_binaries(sys_patch_data.AddIntelGen1Accel, self.constants.legacy_intel_gen1_path)

    def gpu_accel_legacy_sandybridge_master(self):
        if self.constants.detected_os in [os_data.os_data.mojave, os_data.os_data.catalina]:
            print("- Installing Sandy Bridge Acceleration Kext patches for Mojave/Catalina")
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddIntelGen2Accel, self.constants.legacy_intel_gen2_path)
            self.gpu_accel_legacy_sandybridge_board_id()
        elif self.constants.detected_os in [os_data.os_data.big_sur, os_data.os_data.monterey]:
            print("- Installing Sandy Bridge Acceleration Kext patches for Big Sur/Monterey")
            self.delete_old_binaries(sys_patch_data.DeleteNvidiaAccel11)
            self.gpu_accel_legacy()
            self.add_new_binaries(sys_patch_data.AddIntelGen2Accel, self.constants.legacy_intel_gen2_path)
            self.gpu_accel_legacy_sandybridge_board_id()
            self.gpu_accel_legacy_gva()
        else:
            print("- Installing basic Sandy Bridge Framebuffer Kext patches for generic OS")
            self.add_new_binaries(sys_patch_data.AddIntelGen2Accel, self.constants.legacy_intel_gen2_path)
            self.gpu_accel_legacy_sandybridge_board_id()

    def gpu_accel_legacy_sandybridge_board_id(self):
        if self.constants.computer.reported_board_id in self.constants.sandy_board_id_stock:
            print("- Using stock AppleIntelSNBGraphicsFB")
            # TODO: Clean this function up
            # add_new_binaries() and delete_old_binaries() have a bug when the passed array has a single element
            #   'TypeError: expected str, bytes or os.PathLike object, not list'
            # This is a temporary workaround to fix that
            utilities.elevated(["rm", "-r", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB-Clean.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            utilities.elevated(["rm", "-r", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            # Add kext
            print("- Adding AppleIntelSNBGraphicsFB.kext")
            utilities.elevated(
                ["cp", "-r", f"{self.constants.legacy_intel_gen2_path}/AppleIntelSNBGraphicsFB-Clean.kext", f"{self.mount_extensions}"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            # Rename kext
            utilities.elevated(
                ["mv", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB-Clean.kext", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            # Fix permissions
            utilities.elevated(["chown", "-Rf", "root:wheel", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            utilities.elevated(["chmod", "-Rf", "755", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        else:
            # Adjust board ID for spoofs
            print("- Using Board ID patched AppleIntelSNBGraphicsFB")
            utilities.elevated(["rm", "-r", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB-Clean.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            utilities.elevated(["rm", "-r", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            # Add kext
            print("- Adding AppleIntelSNBGraphicsFB.kext")
            utilities.elevated(["cp", "-r", f"{self.constants.legacy_intel_gen2_path}/AppleIntelSNBGraphicsFB.kext", f"{self.mount_extensions}"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            # Fix permissions
            utilities.elevated(["chown", "-Rf", "root:wheel", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            utilities.elevated(["chmod", "-Rf", "755", f"{self.mount_extensions}/AppleIntelSNBGraphicsFB.kext"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def gpu_framebuffer_ivybridge_master(self):
        if self.constants.detected_os == os_data.os_data.monterey:
            print("- Installing IvyBridge Acceleration Kext patches for Monterey")
            self.add_new_binaries(sys_patch_data.AddIntelGen3Accel, self.constants.legacy_intel_gen3_path)
            if self.validate is False:
                print("- Fixing Acceleration in CoreMedia")
                utilities.process_status(subprocess.run(["defaults", "write", "com.apple.coremedia", "hardwareVideoDecoder", "-string", "enable"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
            print("- Merging Ivy Bridge Frameworks")
            utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_frameworks_path_accel_ivy}/", self.mount_frameworks], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            print("- Merging Ivy Bridge PrivateFrameworks")
            utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_private_frameworks_path_accel_ivy}/", self.mount_private_frameworks], stdout=subprocess.PIPE)
        else:
            print("- Installing basic Ivy Bridge Kext patches for generic OS")
            self.add_new_binaries(sys_patch_data.AddIntelGen3Accel, self.constants.legacy_intel_gen3_path)

    def gpu_framebuffer_kepler_master(self):
        if self.constants.detected_os == os_data.os_data.monterey:
            print("- Installing Kepler Acceleration Kext patches for Monterey")
            self.add_new_binaries(sys_patch_data.AddNvidiaKeplerAccel11, self.constants.legacy_nvidia_kepler_path)
            print("- Merging Kepler Frameworks")
            utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_frameworks_path_accel_kepler}/", self.mount_frameworks], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        else:
            print("- Installing Kepler Kext patches for generic OS")
            self.add_new_binaries(sys_patch_data.AddNvidiaKeplerAccel11, self.constants.legacy_nvidia_kepler_path)

    def gpu_accel_legacy_gva(self):
        print("- Merging AppleGVA Hardware Accel patches for non-Metal")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_private_frameworks_path_legacy_drm}/", self.mount_private_frameworks], stdout=subprocess.PIPE)

    def gpu_accel_legacy_extended(self):
        if self.constants.detected_os == os_data.os_data.monterey:
            self.add_legacy_dropbox_patch()

        print("- Merging general legacy Frameworks")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_frameworks_path_accel}/", self.mount_frameworks], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if self.constants.detected_os > os_data.os_data.big_sur:
            print("- Merging Monterey WebKit patch")
            utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_frameworks_path_accel_ivy}/", self.mount_frameworks], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        print("- Merging general legacy PrivateFrameworks")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_private_frameworks_path_accel}/", self.mount_private_frameworks], stdout=subprocess.PIPE)
        if self.constants.detected_os > os_data.os_data.catalina:
            # With PatcherSupportPkg v0.2.0, IOHID-Fixup.plist is deprecated and integrated into SkyLight patch set
            if (Path(self.mount_lauchd) / Path("IOHID-Fixup.plist")).exists():
                print("- Stripping legacy IOHID-Fixup.plist")
                utilities.process_status(
                    utilities.elevated(["rm", "-f", f"{self.mount_lauchd}/IOHID-Fixup.plist"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                )
        else:
            print("- Disabling Library Validation")
            utilities.process_status(
                utilities.elevated(
                    ["defaults", "write", "/Library/Preferences/com.apple.security.libraryvalidation.plist", "DisableLibraryValidation", "-bool", "true"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
            )

    def gpu_accel_legacy_extended_ts2(self):
        print("- Merging TeraScale 2 legacy Frameworks")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_frameworks_path_accel_ts2}/", self.mount_frameworks], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        print("- Merging TeraScale 2 PrivateFrameworks")
        utilities.elevated(["rsync", "-r", "-i", "-a", f"{self.constants.payload_apple_private_frameworks_path_accel_ts2}/", self.mount_private_frameworks], stdout=subprocess.PIPE)
        if self.validate is False:
            print("- Fixing Acceleration in CMIO")
            utilities.process_status(subprocess.run(["defaults", "write", "com.apple.cmio", "CMIO_Unit_Input_ASC.DoNotUseOpenCL", "-bool", "true"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

    def patch_root_vol(self):
        print(f"- Running patches for {self.model}")
        # Graphics patches
        if self.nvidia_legacy is True:
            print("- Installing legacy Nvidia Patches")
            if self.constants.detected_os in self.constants.legacy_accel_support:
                print("- Detected supported OS, installing Acceleration Patches")
                self.added_legacy_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
            self.gpu_accel_legacy_nvidia_master()

        elif self.kepler_gpu is True:
            print("- Installing Kepler Patches")
            if self.constants.detected_os == os_data.os_data.monterey:
                print("- Detected supported OS, installing Acceleration Patches")
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
            self.gpu_framebuffer_kepler_master()

        elif self.amd_ts1 is True:
            print("- Installing legacy TeraScale 1 Patches")
            if self.constants.detected_os in self.constants.legacy_accel_support:
                print("- Detected supported OS, installing Acceleration Patches")
                self.added_legacy_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
            self.gpu_accel_legacy_ts1_master()

        elif self.amd_ts2 is True:
            print("- Installing legacy TeraScale 2 Patches")
            if self.constants.detected_os in self.constants.legacy_accel_support:
                print("- Detected supported OS, installing Acceleration Patches")
                self.added_legacy_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
            self.gpu_accel_legacy_ts2_master()

        if self.iron_gpu is True:
            print("- Installing legacy Ironlake Patches")
            if self.constants.detected_os in self.constants.legacy_accel_support:
                print("- Detected supported OS, installing Acceleration Patches")
                self.added_legacy_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
            self.gpu_accel_legacy_ironlake_master()

        elif self.sandy_gpu is True:
            print("- Installing legacy Sandy Bridge Patches")
            if self.constants.detected_os in self.constants.legacy_accel_support:
                print("- Detected supported OS, installing Acceleration Patches")
                self.added_legacy_kexts = True
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
            self.gpu_accel_legacy_sandybridge_master()

        elif self.ivy_gpu is True:
            print("- Installing Ivy Bridge Patches")
            if self.constants.detected_os == os_data.os_data.monterey:
                print("- Detected supported OS, installing Acceleration Patches")
            else:
                print("- Detected unsupported OS, installing Basic Framebuffer")
            self.gpu_framebuffer_ivybridge_master()

        if self.amd_ts2 is True and self.constants.detected_os in self.constants.legacy_accel_support and self.constants.allow_ts2_accel is True:
            # TeraScale 2 patches must be installed after Intel HD3000
            self.add_new_binaries(sys_patch_data.AddAMDAccel11TS2, self.constants.legacy_amd_path_ts2)

        if self.added_legacy_kexts is True and self.constants.detected_os in self.constants.legacy_accel_support:
            self.gpu_accel_legacy_extended()
            if self.amd_ts2 is True and self.constants.allow_ts2_accel is True:
                self.gpu_accel_legacy_extended_ts2()

        # Misc patches
        if self.brightness_legacy is True:
            print("- Installing legacy Brightness Control")
            self.add_brightness_patch()

        if self.legacy_audio is True:
            print("- Fixing Volume Control Support")
            self.add_audio_patch()

        if self.legacy_wifi is True:
            print("- Installing legacy Wireless support")
            self.add_wifi_patch()

        if self.legacy_gmux is True:
            print("- Installing Legacy Mux Brightness support")
            self.add_legacy_mux_patch()
        
        if self.legacy_keyboard_backlight is True:
            print("- Installing Legacy Keyboard Backlight support")
            self.add_legacy_keyboard_backlight_patch()

        if self.validate is False:
            self.rebuild_snapshot()

    def check_files(self):
        if Path(self.constants.payload_apple_root_path).exists():
            print("- Found Apple Binaries")
            if self.constants.gui_mode is False:
                patch_input = input("Would you like to redownload?(y/n): ")
                if patch_input in {"y", "Y", "yes", "Yes"}:
                    shutil.rmtree(Path(self.constants.payload_apple_root_path))
                    self.download_files()
            else:
                self.download_files()
        else:
            print("- Apple binaries missing")
            self.download_files()

    def download_files(self):
        if self.constants.detected_os == os_data.os_data.monterey:
            os_ver = "12-Monterey"
        elif self.constants.detected_os == os_data.os_data.big_sur:
            os_ver = "11-Big-Sur"
        elif self.constants.detected_os == os_data.os_data.catalina:
            os_ver = "10.15-Catalina"
        elif self.constants.detected_os == os_data.os_data.mojave:
            os_ver = "10.14-Mojave"
        else:
            raise Exception(f"Unsupported OS: {self.constants.detected_os}")
        link = f"{self.constants.url_patcher_support_pkg}{self.constants.patcher_support_pkg_version}/{os_ver}.zip"

        if Path(self.constants.payload_apple_root_path).exists():
            print("- Removing old Apple Binaries folder")
            Path(self.constants.payload_apple_root_path).unlink()
        if Path(self.constants.payload_apple_root_path_zip).exists():
            print("- Removing old Apple Binaries zip")
            Path(self.constants.payload_apple_root_path_zip).unlink()

        download_result = None
        local_zip = Path(self.constants.payload_path) / f"{os_ver}.zip"
        if Path(local_zip).exists():
            print(f"- Found local {os_ver} zip, skipping download")
            print(f"- Duplicating into Apple.zip")
            shutil.copy(local_zip, self.constants.payload_apple_root_path_zip)
            download_result = True
        else:
            download_result = utilities.download_file(link, self.constants.payload_apple_root_path_zip)

        if download_result and self.constants.payload_apple_root_path_zip.exists():
            print("- Download completed")
            print("- Unzipping download...")
            try:
                utilities.process_status(subprocess.run(["unzip", self.constants.payload_apple_root_path_zip], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.constants.payload_path))
                print("- Renaming folder")
                os.rename(self.constants.payload_path / Path(os_ver), self.constants.payload_apple_root_path)
                Path(self.constants.payload_apple_root_path_zip).unlink()
                print("- Binaries downloaded to:")
                print(self.constants.payload_path)
                # if self.constants.gui_mode is False:
                #     input("Press [ENTER] to continue")
            except zipfile.BadZipFile:
                print("- Couldn't unzip")
                return
        else:
            print("- Download failed, please verify the below link works:")
            print(link)
            input("Press [ENTER] to continue")

    def detect_gpus(self):
        gpus = self.constants.computer.gpus
        if self.constants.moj_cat_accel is True:
            non_metal_os = os_data.os_data.high_sierra
        else:
            non_metal_os = os_data.os_data.catalina
        for i, gpu in enumerate(gpus):
            if gpu.class_code and gpu.class_code != 0xFFFFFFFF:
                print(f"- Found GPU ({i}): {utilities.friendly_hex(gpu.vendor_id)}:{utilities.friendly_hex(gpu.device_id)}")
                if gpu.arch in [device_probe.NVIDIA.Archs.Tesla, device_probe.NVIDIA.Archs.Fermi]:
                    if self.constants.detected_os > non_metal_os:
                        self.nvidia_legacy = True
                        self.amfi_must_disable = True
                        # self.legacy_keyboard_backlight = self.check_legacy_keyboard_backlight()
                elif gpu.arch == device_probe.NVIDIA.Archs.Kepler:
                    if self.constants.detected_os > os_data.os_data.big_sur:
                        # Kepler drivers were dropped with Beta 7
                        # 12.0 Beta 5: 21.0.0 - 21A5304g
                        # 12.0 Beta 6: 21.1.0 - 21A5506j
                        # 12.0 Beta 7: 21.1.0 - 21A5522h
                        if self.constants.detected_os == os_data.os_data.monterey and self.constants.detected_os_minor > 0:
                            if "21A5506j" not in self.constants.detected_os_build:
                                self.kepler_gpu = True
                                self.supports_metal = True
                elif gpu.arch == device_probe.AMD.Archs.TeraScale_1:
                    if self.constants.detected_os > non_metal_os:
                        self.amd_ts1 = True
                        self.amfi_must_disable = True
                elif gpu.arch == device_probe.AMD.Archs.TeraScale_2:
                    if self.constants.detected_os > non_metal_os:
                        self.amd_ts2 = True
                        self.amfi_must_disable = True
                elif gpu.arch == device_probe.Intel.Archs.Iron_Lake:
                    if self.constants.detected_os > non_metal_os:
                        self.iron_gpu = True
                        self.amfi_must_disable = True
                elif gpu.arch == device_probe.Intel.Archs.Sandy_Bridge:
                    if self.constants.detected_os > non_metal_os:
                        self.sandy_gpu = True
                        self.amfi_must_disable = True
                        self.check_board_id = True
                elif gpu.arch == device_probe.Intel.Archs.Ivy_Bridge:
                    if self.constants.detected_os > os_data.os_data.big_sur:
                        self.ivy_gpu = True
                        self.supports_metal = True
        if self.supports_metal is True:
            # Avoid patching Metal and non-Metal GPUs if both present, prioritize Metal GPU
            # Main concerns are for iMac12,x with Sandy iGPU and Kepler dGPU
            self.nvidia_legacy = False
            self.amd_ts1 = False
            self.amd_ts2 = False
            self.iron_gpu = False
            self.sandy_gpu = False
    
    def check_dgpu_status(self):
        dgpu = self.constants.computer.dgpu
        if dgpu:
            if dgpu.class_code and dgpu.class_code == 0xFFFFFFFF:
                # If dGPU is disabled via class-codes, assume demuxed
                return False
            return True
        return False

    def detect_demux(self):
        # If GFX0 is missing, assume machine was demuxed
        # -wegnoegpu would also trigger this, so ensure arg is not present
        if not "-wegnoegpu" in (utilities.get_nvram("boot-args") or ""):
            igpu = self.constants.computer.igpu
            dgpu = self.check_dgpu_status()
            if igpu and not dgpu:
                return True
        return False
    
    def check_legacy_keyboard_backlight(self):
        # With Big Sur and newer, Skylight patch set unfortunately breaks native keyboard backlight
        # Penryn Macs are able to re-enable the keyboard backlight by simply running '/usr/libexec/TouchBarServer'
        # For Arrendale and newer, this has no effect.
        if self.model.startswith("MacBookPro") or self.model.startswith("MacBookAir"):
            # non-Metal MacBooks never had keyboard backlight
            if smbios_data.smbios_dictionary[self.model]["CPU Generation"] <= cpu_data.cpu_data.penryn.value:
                if self.constants.detected_os > os_data.os_data.catalina:
                    return True
        return False

    def detect_patch_set(self):
        self.detect_gpus()
        if self.model in model_array.LegacyBrightness:
            if self.constants.detected_os > os_data.os_data.catalina:
                self.brightness_legacy = True

        if self.model in ["iMac7,1", "iMac8,1"] or (self.model in model_array.LegacyAudio and utilities.check_kext_loaded("AppleALC", self.constants.detected_os) is False):
            # Special hack for systems with botched GOPs
            # TL;DR: No Boot Screen breaks Lilu, therefore breaking audio
            if self.constants.detected_os > os_data.os_data.catalina:
                self.legacy_audio = True

        if (
            isinstance(self.constants.computer.wifi, device_probe.Broadcom)
            and self.constants.computer.wifi.chipset in [device_probe.Broadcom.Chipsets.AirPortBrcm4331, device_probe.Broadcom.Chipsets.AirPortBrcm43224]
        ) or (isinstance(self.constants.computer.wifi, device_probe.Atheros) and self.constants.computer.wifi.chipset == device_probe.Atheros.Chipsets.AirPortAtheros40):
            if self.constants.detected_os > os_data.os_data.big_sur:
                self.legacy_wifi = True

        # if self.model in ["MacBookPro5,1", "MacBookPro5,2", "MacBookPro5,3", "MacBookPro8,2", "MacBookPro8,3"]:
        if self.model in ["MacBookPro8,2", "MacBookPro8,3"]:
            # Sierra uses a legacy GMUX control method needed for dGPU switching on MacBookPro5,x
            # Same method is also used for demuxed machines
            # Note that MacBookPro5,x machines are extremely unstable with this patch set, so disabled until investigated further
            # Ref: https://github.com/dortania/OpenCore-Legacy-Patcher/files/7360909/KP-b10-030.txt
            if self.constants.detected_os > os_data.os_data.high_sierra:
                if self.model in ["MacBookPro8,2", "MacBookPro8,3"]:
                    # Ref: https://doslabelectronics.com/Demux.html
                    if self.detect_demux() is True:
                        self.legacy_gmux = True
                else:
                    self.legacy_gmux = True

        utilities.cls()
        print("The following patches will be applied:")
        if self.nvidia_legacy is True:
            print("- Add Legacy Nvidia Tesla Graphics Patch")
        elif self.kepler_gpu is True:
            print("- Add Legacy Nvidia Kepler Graphics Patch")
        elif self.amd_ts1 is True:
            print("- Add Legacy ATI TeraScale 1 Graphics Patch")
        elif self.amd_ts2 is True:
            print("- Add Legacy ATI TeraScale 2 Graphics Patch")
        if self.iron_gpu is True:
            print("- Add Legacy Intel IronLake Graphics Patch")
        elif self.sandy_gpu is True:
            print("- Add Legacy Intel Sandy Bridge Graphics Patch")
        elif self.ivy_gpu is True:
            print("- Add Legacy Intel Ivy Bridge Graphics Patch")
        if self.brightness_legacy is True:
            print("- Add Legacy Brightness Control")
        if self.legacy_audio is True:
            print("- Add legacy Audio Control")
        if self.legacy_wifi is True:
            print("- Add legacy WiFi Control")
        if self.legacy_gmux is True:
            print("- Add Legacy Mux Brightness Control")
        if self.legacy_keyboard_backlight is True:
            print("- Add Legacy Keyboard Backlight Control")

        self.no_patch = not any(
            [
                self.nvidia_legacy,
                self.kepler_gpu,
                self.amd_ts1,
                self.amd_ts2,
                self.iron_gpu,
                self.sandy_gpu,
                self.ivy_gpu,
                self.brightness_legacy,
                self.legacy_audio,
                self.legacy_wifi,
                self.legacy_gmux,
            ]
        )

    def verify_patch_allowed(self):
        sip = sip_data.system_integrity_protection.root_patch_sip_big_sur if self.constants.detected_os > os_data.os_data.catalina else sip_data.system_integrity_protection.root_patch_sip_mojave
        if sip == sip_data.system_integrity_protection.root_patch_sip_mojave:
            sip_value = "For Hackintoshes, please set csr-active-config to '03060000' (0x603)\nFor non-OpenCore Macs, please run 'csrutil disable' in RecoveryOS"
        else:
            sip_value = (
                "For Hackintoshes, please set csr-active-config to '030A0000' (0xA03)\nFor non-OpenCore Macs, please run 'csrutil disable' and \n'csrutil authenticated-root disable' in RecoveryOS"
            )
        self.sip_enabled, self.sbm_enabled, self.amfi_enabled, self.fv_enabled, self.dosdude_patched = utilities.patching_status(sip, self.constants.detected_os)
        if self.sip_enabled is True:
            print("\nCannot patch! Please disable System Integrity Protection (SIP).")
            print("Disable SIP in Patcher Settings and Rebuild OpenCore\n")
            print("Ensure the following bits are set for csr-active-config:")
            print("\n".join(sip))
            print(sip_value)

        if self.sbm_enabled is True:
            print("\nCannot patch! Please disable Apple Secure Boot.")
            print("Disable SecureBootModel in Patcher Settings and Rebuild OpenCore")
            print("For Hackintoshes, set SecureBootModel to Disabled")

        if self.fv_enabled is True:
            print("\nCannot patch! Please disable FileVault.")
            print("For OCLP Macs, please rebuild your config with 0.2.5 or newer")
            print("For others, Go to System Preferences -> Security and disable FileVault")

        if self.amfi_enabled is True and self.amfi_must_disable is True:
            print("\nCannot patch! Please disable AMFI.")
            print("For Hackintoshes, please add amfi_get_out_of_my_way=1 to boot-args")

        if self.check_board_id is True and (self.computer.reported_board_id not in self.constants.sandy_board_id and self.computer.reported_board_id not in self.constants.sandy_board_id_stock):
            print("\nCannot patch! Board ID not supported by AppleIntelSNBGraphicsFB")
            print(f"Detected Board ID: {self.computer.reported_board_id}")
            print("Please ensure your Board ID is listed below:")
            for board in self.constants.sandy_board_id:
                print(f"- {board} ({generate_smbios.find_model_off_board(board)})")
            for board in self.constants.sandy_board_id_stock:
                print(f"- {board} ({generate_smbios.find_model_off_board(board)})")

            self.bad_board_id = True

        if self.dosdude_patched is True:
            print("\nCannot patch! Detected machine has already been patched by another patcher")
            print("Please ensure your install is either clean or patched with OpenCore Legacy Patcher")

        if any(
            [self.sip_enabled, self.sbm_enabled, self.fv_enabled, self.dosdude_patched, self.amfi_enabled if self.amfi_must_disable else False, self.bad_board_id if self.check_board_id else False]
        ):
            return False
        else:
            return True

    # Entry Function
    def start_patch(self):
        print("- Starting Patch Process")
        print(f"- Determining Required Patch set for Darwin {self.constants.detected_os}")
        self.detect_patch_set()
        if self.no_patch is True:
            change_menu = None
            print("- No Root Patches required for your machine!")
            if self.constants.gui_mode is False:
                input("\nPress [ENTER] to return to the main menu: ")
        elif self.constants.gui_mode is False:
            change_menu = input("Would you like to continue with Root Volume Patching?(y/n): ")
        else:
            change_menu = "y"
            print("Continuing root patching")
        if change_menu in ["y", "Y"]:
            print("- Continuing with Patching")
            print("- Verifying whether Root Patching possible")
            if self.verify_patch_allowed() is True:
                print("- Patcher is capable of patching")
                self.check_files()
                self.find_mount_root_vol(True)
            elif self.constants.gui_mode is False:
                input("\nPress [ENTER] to return to the main menu: ")

        else:
            print("- Returning to main menu")

    def start_unpatch(self):
        print("- Starting Unpatch Process")
        if self.verify_patch_allowed() is True:
            self.find_mount_root_vol(False)
            if self.constants.gui_mode is False:
                input("\nPress [ENTER] to return to the main menu")
