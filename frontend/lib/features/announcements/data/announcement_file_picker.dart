import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

class PickedFile {
  const PickedFile({required this.path, required this.name, required this.sizeBytes});
  final String path;
  final String name;
  final int sizeBytes;
}

/// Picks the announcement image (gallery, jpeg/png) or PDF. Behind an
/// interface so tests and the admin form do not touch plugins directly.
///
/// The PDF picker is the one reason the app depends on `file_picker`:
/// image_picker cannot choose documents. It uses the system document UI (no
/// storage permission) and only returns the file the user selected.
abstract class AnnouncementFilePicker {
  Future<PickedFile?> pickImage();
  Future<PickedFile?> pickPdf();
}

class PluginAnnouncementFilePicker implements AnnouncementFilePicker {
  const PluginAnnouncementFilePicker();

  @override
  Future<PickedFile?> pickImage() async {
    try {
      final f = await ImagePicker().pickImage(
        source: ImageSource.gallery,
        maxWidth: 2048,
        maxHeight: 2048,
        imageQuality: 85,
      );
      if (f == null) return null;
      return PickedFile(path: f.path, name: f.name, sizeBytes: await File(f.path).length());
    } on Exception {
      return null;
    }
  }

  @override
  Future<PickedFile?> pickPdf() async {
    try {
      final r = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: const ['pdf'],
      );
      final f = r?.files.single;
      final path = f?.path;
      if (f == null || path == null) return null;
      return PickedFile(path: path, name: f.name, sizeBytes: f.size);
    } on Exception {
      return null;
    }
  }
}

final announcementFilePickerProvider =
    Provider<AnnouncementFilePicker>((ref) => const PluginAnnouncementFilePicker());
