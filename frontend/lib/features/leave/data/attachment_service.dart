import 'dart:io';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

class PickedAttachment {
  const PickedAttachment(this.path, this.name);
  final String path;
  final String name;
}

enum AttachmentSource { camera, gallery }

/// Picks a supporting document as a photo (jpeg/png, downscaled) using the
/// image_picker package already in the app. PDFs are not offered: that would
/// need file_picker (a larger dependency with broad storage access), so a photo
/// or scan of the document is used instead.
abstract class AttachmentService {
  Future<PickedAttachment?> pick(AttachmentSource source);

  /// Deletes the local copy (after upload or when removed). Never throws.
  Future<void> discard(String path);
}

class ImagePickerAttachmentService implements AttachmentService {
  const ImagePickerAttachmentService();

  @override
  Future<PickedAttachment?> pick(AttachmentSource source) async {
    try {
      final f = await ImagePicker().pickImage(
        source: source == AttachmentSource.camera ? ImageSource.camera : ImageSource.gallery,
        maxWidth: 1600,
        maxHeight: 1600,
        imageQuality: 80,
      );
      if (f == null) return null;
      return PickedAttachment(f.path, f.name);
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
}

final attachmentServiceProvider =
    Provider<AttachmentService>((ref) => const ImagePickerAttachmentService());
