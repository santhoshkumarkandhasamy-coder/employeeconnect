import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../domain/phase5b_models.dart';

/// A PDF chosen for a Form 16 upload, held in memory only (no path kept). When [error] is set the file
/// was refused before any byte was read ([bytes] is empty).
class PickedPdf {
  const PickedPdf({required this.name, required this.bytes, this.error});
  final String name;
  final Uint8List bytes;
  final String? error;

  bool get ok => error == null;
  String get sha256Hex => sha256.convert(bytes).toString();
}

/// Behind an interface so tests do not touch the plugin.
abstract class Form16Picker {
  /// Up to [form16MaxFiles] PDFs; an empty list means nothing was chosen.
  Future<List<PickedPdf>> pickPdfs();
}

class PluginForm16Picker implements Form16Picker {
  const PluginForm16Picker();

  @override
  Future<List<PickedPdf>> pickPdfs() async {
    try {
      final r = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: const ['pdf'],
        allowMultiple: true,
        withData: false,
        withReadStream: true,
      );
      if (r == null) return const [];
      if (r.files.length > form16MaxFiles) {
        return [PickedPdf(name: '', bytes: Uint8List(0), error: 'Choose at most $form16MaxFiles files at a time.')];
      }
      final out = <PickedPdf>[];
      var total = 0;
      for (final f in r.files) {
        // Size first, bytes only if the file fits: a huge file cannot exhaust memory.
        if (f.size > form16MaxFileBytes) {
          out.add(PickedPdf(name: f.name, bytes: Uint8List(0), error: 'Each file must be 2 MB or smaller.'));
          continue;
        }
        total += f.size;
        final stream = f.readStream;
        if (stream == null) continue;
        final buf = BytesBuilder(copy: false);
        var tooBig = false;
        await for (final chunk in stream) {
          buf.add(chunk);
          if (buf.length > form16MaxFileBytes) {
            tooBig = true;
            break;
          }
        }
        out.add(tooBig
            ? PickedPdf(name: f.name, bytes: Uint8List(0), error: 'Each file must be 2 MB or smaller.')
            : PickedPdf(name: f.name, bytes: buf.takeBytes()),);
      }
      if (total > form16MaxTotalBytes) {
        return [PickedPdf(name: '', bytes: Uint8List(0), error: 'The files together must be 40 MB or smaller.')];
      }
      return out;
    } on Exception {
      return const [];
    }
  }
}

final form16PickerProvider = Provider<Form16Picker>((ref) => const PluginForm16Picker());
