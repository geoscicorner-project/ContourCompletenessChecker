# -*- coding: utf-8 -*-
def classFactory(iface):
    from .contour_completeness_checker import ContourCompletenessChecker
    return ContourCompletenessChecker(iface)
