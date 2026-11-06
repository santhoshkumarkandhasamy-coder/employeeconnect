import 'package:flutter/material.dart';

import 'pay_items_widgets.dart';

/// Small pieces shared by the tax screens.

PayTone proofTone(String status) => switch (status) {
      'verified' => PayTone.good,
      'submitted' => PayTone.warn,
      'rejected' => PayTone.bad,
      _ => PayTone.neutral,
    };

PayTone rulesTone(String status) => switch (status) {
      'verified' => PayTone.good,
      'submitted' => PayTone.warn,
      _ => PayTone.neutral,
    };

PayTone form16Tone(String status) => switch (status) {
      'published' => PayTone.good,
      'uploaded' => PayTone.warn,
      'withdrawn' => PayTone.bad,
      _ => PayTone.neutral,
    };

class TaxStatusChip extends StatelessWidget {
  const TaxStatusChip(this.label, {required this.tone, super.key});
  final String label;
  final PayTone tone;

  @override
  Widget build(BuildContext context) => PayStatusChip(label, tone: tone);
}
