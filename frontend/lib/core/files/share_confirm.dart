import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Asks once per session (until logout or lock reset [confirmed], see
/// `shareConfirmedProvider`) before a file with personal data of employees is
/// handed to another app. Returns true when sharing may go on. Review 08 L7.
///
/// Pass `ref.read(shareConfirmedProvider.notifier)` captured before any await.
Future<bool> confirmShareOnce(BuildContext context, StateController<bool> confirmed) async {
  if (confirmed.state) return true;
  final ok = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: const Text('Share this file?'),
      content: const Text(
        'This file contains personal data of employees. Once you pick an app, '
        'this app can no longer protect it. Only share it with apps and people '
        'you trust, and delete it afterwards.',
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
        FilledButton(
          onPressed: () => Navigator.pop(ctx, true),
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: const Text('Continue'),
        ),
      ],
    ),
  );
  if (ok == true) confirmed.state = true;
  return ok == true;
}
