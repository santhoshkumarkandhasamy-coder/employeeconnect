import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

import '../domain/claims_models.dart';

enum BillSource { camera, gallery, document }

/// A bill chosen for a claim, held in memory only (no path kept). When
/// [error] is set the file was refused BEFORE any byte was read ([bytes] is
/// empty): wrong type or over 10 MB.
class PickedBill {
  const PickedBill({required this.name, required this.bytes, this.error});
  final String name;
  final Uint8List bytes;
  final String? error;

  bool get ok => error == null;
}

/// Behind an interface so tests do not touch the plugins.
abstract class ClaimBillPicker {
  Future<PickedBill?> pick(BillSource source);
}

class PluginClaimBillPicker implements ClaimBillPicker {
  const PluginClaimBillPicker();

  @override
  Future<PickedBill?> pick(BillSource source) async {
    try {
      return source == BillSource.camera ? await _camera() : await _files(source);
    } on Exception {
      return null; // permission denied, no camera, picker closed
    }
  }

  Future<PickedBill?> _camera() async {
    final f = await ImagePicker().pickImage(
      source: ImageSource.camera,
      maxWidth: 2000,
      maxHeight: 2000,
      imageQuality: 85,
    );
    if (f == null) return null;
    final name = f.name.contains('.') ? f.name : '${f.name}.jpg';
    final size = await f.length(); // size first, bytes only if it fits
    final err = validateClaimBill(name: name, size: size);
    if (err != null) return PickedBill(name: name, bytes: Uint8List(0), error: err);
    return PickedBill(name: name, bytes: await f.readAsBytes());
  }

  Future<PickedBill?> _files(BillSource source) async {
    // The size is checked before any byte is loaded and the stream is read
    // with a hard cap, so a huge file cannot exhaust memory.
    final r = await FilePicker.platform.pickFiles(
      type: source == BillSource.gallery ? FileType.image : FileType.custom,
      allowedExtensions: source == BillSource.gallery ? null : const ['pdf', 'jpg', 'jpeg', 'png'],
      withData: false,
      withReadStream: true,
    );
    final f = r?.files.single;
    if (f == null) return null;
    final err = validateClaimBill(name: f.name, size: f.size);
    if (err != null) return PickedBill(name: f.name, bytes: Uint8List(0), error: err);
    final stream = f.readStream;
    if (stream == null) return null;
    final buf = BytesBuilder(copy: false);
    await for (final chunk in stream) {
      buf.add(chunk);
      if (buf.length > claimBillMaxBytes) {
        return PickedBill(
          name: f.name,
          bytes: Uint8List(0),
          error: 'The file is too large (10 MB at most).',
        );
      }
    }
    return PickedBill(name: f.name, bytes: buf.takeBytes());
  }
}

final claimBillPickerProvider = Provider<ClaimBillPicker>((ref) => const PluginClaimBillPicker());
