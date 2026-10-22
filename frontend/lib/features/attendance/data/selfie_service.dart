import 'dart:io';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';

/// Captures a selfie with the camera, downscaled to 1024 px and compressed.
///
/// The photo never leaves app-private storage: it is moved into
/// `<app cache>/ec_selfie` (Directory.systemTemp is the app's private cache
/// dir on Android and the app sandbox tmp dir on iOS, never shared storage or
/// the gallery) and must be deleted with [discard] once uploaded.
abstract class SelfieService {
  /// Returns the local file path, or null if the user cancelled / no camera.
  Future<String?> capture();

  /// Deletes a captured selfie. Safe to call for a missing file.
  Future<void> discard(String path);

  /// Deletes every leftover selfie (crash, logout).
  Future<void> purgeAll();
}

class ImagePickerSelfieService implements SelfieService {
  const ImagePickerSelfieService();

  static Directory get _dir =>
      Directory('${Directory.systemTemp.path}${Platform.pathSeparator}ec_selfie');

  @override
  Future<String?> capture() async {
    try {
      await purgeAll(); // leftovers from a killed run
      final f = await ImagePicker().pickImage(
        source: ImageSource.camera,
        preferredCameraDevice: CameraDevice.front,
        maxWidth: 1024,
        maxHeight: 1024,
        imageQuality: 80,
      );
      if (f == null) return null;
      _dir.createSync(recursive: true);
      final dest = '${_dir.path}${Platform.pathSeparator}'
          'selfie_${DateTime.now().microsecondsSinceEpoch}.jpg';
      await File(f.path).copy(dest);
      await discard(f.path); // remove the picker's own copy
      return dest;
    } on Exception {
      return null; // permission denied or no camera
    }
  }

  @override
  Future<void> discard(String path) async {
    try {
      final f = File(path);
      if (f.existsSync()) await f.delete();
    } on Object {
      // best effort
    }
  }

  @override
  Future<void> purgeAll() async {
    try {
      if (_dir.existsSync()) await _dir.delete(recursive: true);
    } on Object {
      // best effort
    }
  }
}

final selfieServiceProvider = Provider<SelfieService>((ref) {
  const svc = ImagePickerSelfieService();
  ref.listen<AuthState>(authControllerProvider, (_, next) {
    if (next is AuthUnauthenticated || next is AuthLocked) svc.purgeAll();
  });
  return svc;
});
