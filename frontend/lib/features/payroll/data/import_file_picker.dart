import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Largest workbook the server accepts (contract 17.2).
const int payrollImportMaxBytes = 1024 * 1024;

/// A workbook chosen for bulk import, held in memory only (no path, so it also
/// works in the browser console).
class PickedWorkbook {
  const PickedWorkbook(
      {required this.name, required this.bytes, this.tooLarge = false});
  final String name;
  final Uint8List bytes;

  /// The file is over [payrollImportMaxBytes]; it was NOT read ([bytes] is empty).
  final bool tooLarge;

  /// Lower-case hex SHA-256 of the bytes (compared with the dry run's).
  String get sha256Hex => sha256.convert(bytes).toString();
}

/// Behind an interface so tests do not touch the plugin.
abstract class PayrollImportPicker {
  Future<PickedWorkbook?> pickWorkbook();
}

class PluginPayrollImportPicker implements PayrollImportPicker {
  const PluginPayrollImportPicker();

  @override
  Future<PickedWorkbook?> pickWorkbook() async {
    try {
      // The size is checked before any byte is loaded, and the stream is read with a hard cap, so a huge file
      // cannot exhaust memory in the app or the browser.
      final r = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: const ['xlsx'],
        withData: false,
        withReadStream: true,
      );
      final f = r?.files.single;
      if (f == null) return null;
      if (f.size > payrollImportMaxBytes) {
        return PickedWorkbook(
            name: f.name, bytes: Uint8List(0), tooLarge: true);
      }
      final stream = f.readStream;
      if (stream == null) return null;
      final buf = BytesBuilder(copy: false);
      await for (final chunk in stream) {
        buf.add(chunk);
        if (buf.length > payrollImportMaxBytes) {
          return PickedWorkbook(
              name: f.name, bytes: Uint8List(0), tooLarge: true);
        }
      }
      return PickedWorkbook(name: f.name, bytes: buf.takeBytes());
    } on Exception {
      return null;
    }
  }
}

final payrollImportPickerProvider =
    Provider<PayrollImportPicker>((ref) => const PluginPayrollImportPicker());
