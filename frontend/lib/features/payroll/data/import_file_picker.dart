import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Largest workbook the server accepts (contract 17.2).
const int payrollImportMaxBytes = 1024 * 1024;

/// A workbook chosen for bulk import, held in memory only (no path, so it also
/// works in the browser console).
class PickedWorkbook {
  const PickedWorkbook({required this.name, required this.bytes});
  final String name;
  final Uint8List bytes;

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
      final r = await FilePicker.platform.pickFiles(
        type: FileType.custom,
        allowedExtensions: const ['xlsx'],
        withData: true,
      );
      final f = r?.files.single;
      final data = f?.bytes;
      if (f == null || data == null) return null;
      return PickedWorkbook(name: f.name, bytes: data);
    } on Exception {
      return null;
    }
  }
}

final payrollImportPickerProvider =
    Provider<PayrollImportPicker>((ref) => const PluginPayrollImportPicker());
