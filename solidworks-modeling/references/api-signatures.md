<!-- 由 sw_make_cheatsheet 从 SW2024 官方 API 帮助生成；共 145 个成员 -->

## ISldWorks

- `ISldWorks.OpenDoc6(FileName: str, Type: int, Options: int, Configuration: str, Errors: int, Warnings: int)` → `ModelDoc2`
    - FileName: Document name or full path if not in current directory, including extension
    - Type: Document type as defined in swDocumentTypes_e
    - Options: Mode in which to open the document as defined in swOpenDocOptions_e
    - Configuration: Model configuration in which to open this document
 
 Applies to parts and assemblies, not drawings
 
 If this argument is empty or the specified conf
    - Errors: Load errors as defined in swFileLoadError_e (See Remarks)
    - Warnings: Warnings or extra information generated during the open operation as defined in swFileLoadWarning_e
    - 说明: Opens an existing document and returns a pointer to the document object.
- `ISldWorks.NewDocument(TemplateName: str, PaperSize: int, Width: double, Height: double)` → `Object`
    - TemplateName: Fully qualified path and name of the template to use for creating the new document
    - PaperSize: Size of paper as defined in swDwgPaperSizes_e
    - Width: Width of paper; used only when PaperSize is swDwgPapersUserDefined
    - Height: Height of paper; used only when PaperSize is swDwgPapersUserDefined
    - 说明: Creates a new document based on the specified template.
- `ISldWorks.CloseDoc()` → `?`
    - 说明: Closes the specified document.
- `ISldWorks.ActivateDoc3(Name: str, UseUserPreferences: bool, Option: int, Errors: int)` → `Object`
    - Name: Name of the loaded document to activate (see Remarks)
    - UseUserPreferences: True to rebuild as per the swRebuildOnActivation system option; false to rebuild as per Option
    - Option: Rebuild option as defined in swRebuildOnActivation_e (see Remarks)
    - Errors: Status of the activate operation as defined in swActivateDocError_e; if
 no errors or warnings are encountered, then this value is 0 (see Remarks)
    - 说明: Activates a loaded document and rebuilds it as specified.
- `ISldWorks.RevisionNumber()` → `str`
    - 说明: Gets the version number of this SOLIDWORKS installation.
- `ISldWorks.GetUserPreferenceStringValue(UserPreference: int)` → `str`
    - UserPreference: User preference as defined in swUserPreferenceStringValue_e
    - 说明: Gets system default user preference values.
- `ISldWorks.SetUserPreferenceToggle()` → `?`
    - 说明: Sets system default user preference values.
- `ISldWorks.GetUserPreferenceIntegerValue(UserPreferenceValue: int)` → `int`
    - UserPreferenceValue: User preference as defined in swUserPreferenceIntegerValue_e
    - 说明: Gets system default user preference values.
- `ISldWorks.GetUserPreferenceDoubleValue(UserPreferenceValue: int)` → `double`
    - UserPreferenceValue: User preference as defined in swUserPreferenceDoubleValue_e
    - 说明: Gets system default user preference values.
- `ISldWorks.CommandInProgress()` → `bool`
    - 说明: Improves performance of out-of-process applications by informing SOLIDWORKS that a sequence of API calls will be made by the out-of-process application.
- `ISldWorks.LoadFile4(FileName: str, ArgString: str, ImportData: Object, Errors: int)` → `ModelDoc2`
    - FileName: Full path and file name of the third-party native CAD part or assembly document to import (see Remarks)
    - ArgString: Space-separated string that allows optional arguments to be specified when opening a third-party native CAD file; empty string if 3D Interconnect is e
    - ImportData: IImportIgesData, IImportDxfDwgData, or IImportStepData object; null or Nothing if 3D Interconnect is not enabled (see Remarks)
    - Errors: Errors as defined in swFileLoadError_e
    - 说明: Loads a third-party native CAD file into a new SOLIDWORKS document using 3D Interconnect.
- `ISldWorks.GetCurrentLanguage()` → `str`
    - 说明: Gets the current language used by SOLIDWORKS.
- `ISldWorks.IActiveDoc2()` → `ModelDoc2`
    - 说明: Gets the currently active document.

## IModelDoc2

- `IModelDoc2.SketchManager()` → `SketchManager`
    - 说明: Gets the sketch manager, which allows access to sketch-creation routines.
- `IModelDoc2.FeatureManager()` → `FeatureManager`
    - 说明: Gets the IFeatureManager object, which allows access to the FeatureManager design tree.
- `IModelDoc2.Extension()` → `ModelDocExtension`
    - 说明: Gets the IModelDocExtension object, which also allows access to the model document.
- `IModelDoc2.SelectionManager()` → `Object`
    - 说明: Gets the ISelectionMgr object for this document, which makes the currently selected object available.
- `IModelDoc2.ClearSelection2()` → `?`
    - 说明: Clears the selection list.
- `IModelDoc2.EditRebuild3()` → `bool`
    - 说明: Rebuilds only those features that need to be rebuilt in the active configuration in the model.
- `IModelDoc2.ForceRebuild3(TopOnly: bool)` → `bool`
    - TopOnly: True rebuilds the top-level assembly only; false rebuilds the top-level assembly and all subassemblies
    - 说明: Forces a rebuild of all features in the active configuration in the model.
- `IModelDoc2.GetActiveSketch2()` → `Object`
    - 说明: Obsolete. Superseded by SketchManager::ActiveSketch.
- `IModelDoc2.Parameter(StringIn: str)` → `Object`
    - StringIn: Name of parameter (for example, "D1@Base-Revolve")
    - 说明: Gets the specified parameter.
- `IModelDoc2.InsertSketch2()` → `?`
    - 说明: Obsolete. Superseded by ISketchManager::InsertSketch.
- `IModelDoc2.CreateCircularSketchStepAndRepeat(ArcRadius: double, ArcAngle: double, PatternNum: int, PatternSpacing: double, PatternRotate: bool, DeleteInstances: str)` → `bool`
    - ArcRadius: Radius to be used in the circular sketch pattern
    - ArcAngle: Angle relative to the sketch entities being patterned
    - PatternNum: Total number of instances, including the seed geometry
    - PatternSpacing: Spacing between pattern instances in radians
    - PatternRotate: True to rotate the pattern, false to not
    - DeleteInstances: Number of instances to delete passed as a string formatted as: "(a) (b) (c) "
    - 说明: Obsolete. Superseded by ISketchManager::CreateCircularSketchStepAndRepeat.
- `IModelDoc2.CreateLinearSketchStepAndRepeat(NumX: int, NumY: int, SpacingX: double, SpacingY: double, AngleX: double, AngleY: double, DeleteInstances: str)` → `bool`
    - NumX: Total number of instances along direction 1, including the seed
    - NumY: Total number of instances along direction 2, including the seed
    - SpacingX: Spacing between instances along direction 1
    - SpacingY: Spacing between instances along direction 2
    - AngleX: Relative to the X axis, the angle for direction 1
    - AngleY: Relative to the X axis, the angle for direction 2
    - DeleteInstances: Number of instances to delete passed as a string in the format: "(a) (b) (c) "
    - 说明: Obsolete. Superseded by ISketchManager::CreateLinearSketchStepAndRepeat.
- `IModelDoc2.Save3(Options: int, Errors: int, Warnings: int)` → `bool`
    - Options: Mode in which to save the document as defined in swSaveAsOptions_e
    - Errors: Errors that caused the save operation to fail as defined in swFileSaveError_e
    - Warnings: Warnings or extra information generated during the save operation as defined in swFileSaveWarning_e
    - 说明: Saves the current document.
- `IModelDoc2.SaveAs3(NewName: str, SaveAsVersion: int, Options: int)` → `int`
    - 说明: Obsolete. Superseded by IModelDocExtension::SaveAs.
- `IModelDoc2.SaveAs4(Name: str, Version: int, Options: int, Errors: int, Warnings: int)` → `bool`
    - 说明: Obsolete. Superseded by IModelDocExtension::SaveAs.
- `IModelDoc2.GetTitle()` → `str`
    - 说明: Gets the title of the document that appears in the active window's title bar.
- `IModelDoc2.GetPathName()` → `str`
    - 说明: Gets the full path name for this document, including the file name.
- `IModelDoc2.GetType()` → `int`
    - 说明: Gets the type of the document.
- `IModelDoc2.FirstFeature()` → `Object`
    - 说明: Gets the first feature in the document.
- `IModelDoc2.ViewZoomtofit2()` → `?`
    - 说明: Zooms the currently active view to fit the screen.
- `IModelDoc2.InsertSketch()` → `?`
    - 说明: Obsolete. Superseded by ISketchManager::InsertSketch.
- `IModelDoc2.ShowNamedView2()` → `?`
    - 说明: Shows the specified view.
- `IModelDoc2.SetPickMode()` → `?`
    - 说明: Returns the user to the default selection mode.
- `IModelDoc2.GetActiveSketch()` → `Object`
    - 说明: Obsolete. Superseded by IModelDoc2::GetActiveSketch2.
- `IModelDoc2.SketchManager()` → `SketchManager`
    - 说明: Gets the sketch manager, which allows access to sketch-creation routines.
- `IModelDoc2.ModelViewManager()` → `ModelViewManager`
    - 说明: Gets the IModelViewManager object, which allows access to the model view.
- `IModelDoc2.ConfigurationManager()` → `ConfigurationManager`
    - 说明: Gets the IConfigurationManager object, which allows access to a configuration in a model.
- `IModelDoc2.Extension()` → `ModelDocExtension`
    - 说明: Gets the IModelDocExtension object, which also allows access to the model document.
- `IModelDoc2.GetConfigurationByName(Name: str)` → `Object`
    - Name: Configuration name
    - 说明: Gets the specified configuration.
- `IModelDoc2.AddConfiguration3(Name: str, Comment: str, AlternateName: str, Options: int)` → `Object`
    - Name: Name of the new configuration
    - Comment: Comment displayed in Configuration Properties
    - AlternateName: Alternate configuration name; used if swConfigOption_UseAlternateName is set to true
    - Options: Combination of one or more configuration options as defined in swConfigurationOptions2_e
    - 说明: Adds a new configuration to this model document.
- `IModelDoc2.SelectByID(SelID: str, SelParams: str, X: double, Y: double, Z: double)` → `bool`
    - 说明: Obsolete. Superseded by IModelDocExtension::SelectByID2.

## IModelDocExtension

- `IModelDocExtension.SelectByID2(Name: str, Type: str, X: double, Y: double, Z: double, Append: bool, Mark: int, Callout: Callout, SelectOption: int)` → `bool`
    - Name: Name of object to select or an empty string
    - Type: Type of object (uppercase) as defined in swSelectType_e or an empty string
    - X: X selection location or 0
    - Y: Y selection location or 0
    - Z: Z selection location or 0
    - Append: If... 
 An, if entity is... 
 Then... 
 
 
 True 
 Not already selected 
 Entity is appended to the current selection list 
 
 Already selected 
 Enti
    - Mark: Value that you want to use as a mark; this value is used by other functions that require ordered selection (see Remarks)
    - Callout: Pointer to the associated callout
    - SelectOption: Selection option as defined in swSelectOption_e (see Remarks)
    - 说明: Selects the specified entity.
- `IModelDocExtension.SaveAs(Name: str, Version: int, Options: int, ExportData: Object, Errors: int, Warnings: int)` → `bool`
    - Name: Full pathname of the document to save; the file extension indicates any conversion that should be performed (for example, Part1.igs to save in IGES fo
    - Version: Format in which to save this document as defined in swSaveAsVersion_e (see Remarks)
    - Options: Option indicating how to save the document as defined in swSaveAsOptions_e (see Remarks)
    - ExportData: IExportPdfData object for exporting drawing sheets to PDF (see Remarks)
    - Errors: Errors that caused the save to fail as defined in swFileSaveError_e (see Remarks)
    - Warnings: Warnings or extra information generated during the save operation as defined in swFileSaveWarning_e (see Remarks)
    - 说明: Obsolete. Superseded by IModelDocExtension::SaveAs2.
- `IModelDocExtension.CreateMassProperty2()` → `Object`
    - 说明: Creates a mass properties object.
- `IModelDocExtension.GetMassProperties(Accuracy: int, Status: int)` → `Object`
    - Accuracy: 0 = as is
 
 1 = default
 
 2 = maximum
    - Status: Status of the mass property results as defined in swMassPropertiesStatus_e
    - 说明: Obsolete. Superseded by IModelDocExtension::GetMassProperties2.
- `IModelDocExtension.CustomPropertyManager()` → `?`
    - 说明: Gets the custom properties for this document or configuration.
- `IModelDocExtension.AddOrUpdateSearchData(AppName: str, AppKeyword: str, AppValue: str)` → `bool`
    - AppName: Third-party application name
    - AppKeyword: Third-party application keyword
    - AppValue: Value for AppKeyword
    - 说明: Adds or updates the SOLIDWORKS Search, third-party, application keyword and value to the model document.
- `IModelDocExtension.InsertGeneralTableAnnotation(UseAnchorPoint: bool, X: double, Y: double, AnchorType: int, TableTemplate: str, Rows: int, Columns: int)` → `TableAnnotation`
    - UseAnchorPoint: True to anchor the table by AnchorType and ignore any coordinates specified by X and Y, false to use the coordinates specified by X and Y
    - X: X coordinate of this table annotation
    - Y: Y coordinate of this table annotation
    - AnchorType: Type of anchor as defined in swBOMConfigurationAnchorType_e; valid only if UseAnchorPoint is true and TableTemplate is empty (see Remarks)
    - TableTemplate: Path and file name of the general table template to use }}-->}}-->(see Remarks)
    - Rows: Number of rows in the table annotation; valid only if TableTemplate is empty (see Remarks)
    - Columns: Number of columns in the table annotation; valid only if TableTemplate is empty (see Remarks)
    - 说明: Inserts the a general table annotation in this model document.
- `IModelDocExtension.GetObjectId(Annotation: Annotation)` → `int`
    - Annotation: Annotation
    - 说明: Gets the object ID for the specified annotation.
- `IModelDocExtension.RunCommand(CommandID: int, NewTitle: str)` → `bool`
    - CommandID: SOLIDWORKS command as defined in swCommands_e
    - NewTitle: Your title for the SOLIDWORKS PropertyManager page, if invoked by this command
    - 说明: Runs the specified SOLIDWORKS command.
- `IModelDocExtension.SetUserPreferenceDouble(UserPref: int, UserPrefOption: int, Value: double)` → `bool`
    - UserPref: User preference as defined in swUserPreferenceDoubleValue_e
    - UserPrefOption: User-preference option for customizing annotation and dimension drafting standards and as defined in swUserPreferenceOption_e; if not needed, then spe
    - Value: Value to assign to the user preference specified in UserPref
    - 说明: Sets document default user preference values. This method is intended for user preferences of type double.

## ISelectionMgr

- `ISelectionMgr.GetSelectedObject6(Index: int, Mark: int)` → `Object`
    - Index: Index position within the current list of selected items, where Index ranges from 1 to ISelectionMgr::GetSelectedObjectCount2 (see Remarks)
    - Mark: -1 = All selections regardless of marks
 
 0 = only the selections without marks
 Any other value = Value that was used to mark and select an object
    - 说明: Gets the selected object.
- `ISelectionMgr.GetSelectedObjectType3(Index: int, Mark: int)` → `int`
    - Index: Index position with in the current list of selected items, where AtIndex ranges from 1 to ISelectionMgr::GetSelectedObjectCount2 (see Remarks)
    - Mark: -1 = All selections regardless of marks
 0 = only the selections without marks
 Any other value = Value that was used to mark and select an object
    - 说明: Gets the type of object selected.
- `ISelectionMgr.GetSelectedObjectCount2(Mark: int)` → `int`
    - Mark: -1 = All selections regardless of marks
 0 = only the selections without marks
 Any other value = Value that was used to mark and select an object
    - 说明: Gets the number of selected objects.
- `ISelectionMgr.GetSelectionPoint2(Index: int, Mark: int)` → `Object`
    - Index: Index position with in the current list of selected items, where AtIndex ranges from 1 to ISelectionMgr::GetSelectedObjectCount2 (see Remarks)
    - Mark: -1 = All selections regardless of marks
 0 = only the selections without marks
 Any other value = Value that was used to mark and select an object
    - 说明: Gets the selected point in model space coordinates from the currently selected object.
- `ISelectionMgr.SuspendSelectionList()` → `int`
    - 说明: Suspends the current selection list.
- `ISelectionMgr.ResumeSelectionList2()` → `?`
    - 说明: Reinstates the previously suspended selection list.
- `ISelectionMgr.GetSelectedObjectsComponent4(Index: int, Mark: int)` → `Object`
    - Index: Index position within the current list of selected items, where the index ranges from 1 to ISelectionMgr::GetSelectedObjectCount2 (see Remarks)
    - Mark: -1 = All selections regardless of marks
 0 = Only the selections without marks
 Any other value = Value that was used to mark and select an object
    - 说明: Gets the selected component in an assembly or drawing.

## ISketchManager

- `ISketchManager.InsertSketch()` → `?`
    - 说明: Inserts a new sketch in the current part or assembly document.
- `ISketchManager.CreateCircleByRadius(XC: double, YC: double, Zc: double, Radius: double)` → `SketchSegment`
    - XC: X coordinate of the circle center point in meters
    - YC: Y coordinate of the circle center point in meters
    - Zc: Z coordinate of the circle center point in meters
    - Radius: Radius of the circle in meters
    - 说明: Creates a circle based on a center point and a specified radius.
- `ISketchManager.CreateLine(X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double)` → `SketchSegment`
    - X1: X coordinate of the line start point
    - Y1: Y coordinate of the line start point
    - Z1: Z coordinate of the line start point
    - X2: X coordinate of the line end point
    - Y2: Y coordinate of the line end point
    - Z2: Z coordinate of the line end point
    - 说明: Creates a sketch line in the currently active 2D or 3D sketch.
- `ISketchManager.CreateCornerRectangle(X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double)` → `Object`
    - X1: Upper-left X coordinate for point 1
    - Y1: Upper-left Y coordinate for point 1
    - Z1: Upper-left Z coordinate for point 1
    - X2: Lower-right X coordinate for point 2
    - Y2: Lower-right Y coordinate for point 2
    - Z2: Lower-right Z coordinate for point 2
    - 说明: Creates a corner rectangle.
- `ISketchManager.CreateCenterRectangle(X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double)` → `Object`
    - X1: X coordinate for point 1
    - Y1: Y coordinate for point 1
    - Z1: Z coordinate for point 1
    - X2: X coordinate for point 2
    - Y2: Y coordinate for point 2
    - Z2: Z coordinate for point 2
    - 说明: Creates a center rectangle.
- `ISketchManager.CreateArc(XC: double, YC: double, Zc: double, X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double, Direction: Short)` → `SketchSegment`
    - XC: X coordinate of the circle center point in meters
    - YC: coordinate of the circle center point in meters
    - Zc: Z coordinate of the circle center point in meters
    - X1: X coordinate of the start point of the arc in meters
    - Y1: coordinate of the start point of the arc in meters
    - Z1: Z coordinate of the start point of the arc in meters
    - X2: X coordinate of the end point of the arc in meters
    - Y2: Y coordinate of the end point of the arc in meters
    - Z2: coordinate of the end point of the arc in meters
    - Direction: +1 : Go from the start point to the end point in a counter-clockwise direction
 -1 : Go from the start point to the end point in a clockwise direction
    - 说明: Creates an arc based on a center point, a start point, an end point, and a direction.
- `ISketchManager.Create3PointArc(X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double, X3: double, Y3: double, Z3: double)` → `SketchSegment`
    - X1: X coordinate of point 1
    - Y1: Y coordinate of point 1
    - Z1: Z coordinate of point 1
    - X2: X coordinate of point 2
    - Y2: Y coordinate of point 2
    - Z2: Z coordinate of point 2
    - X3: X coordinate of point 3
    - Y3: Y coordinate of point 3
    - Z3: Z coordinate of point 3
    - 说明: Creates a 3-point arc.
- `ISketchManager.CreateTangentArc(X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double, ArcType: int)` → `SketchSegment`
    - X1: X coordinate of start point in meters
    - Y1: Y coordinate of start point in meters
    - Z1: Z coordinate of start point in meters
    - X2: X coordinate of end point in meters
    - Y2: Y coordinate of end point in meters
    - Z2: Z coordinate of end point in meters
    - ArcType: Type of tangent arc as defined in swTangentArcTypes_e
    - 说明: Creates a tangent arc.
- `ISketchManager.CreateSpline(PointData: Object)` → `SketchSegment`
    - PointData: Array of X,Y,Z point coordinates to use in creating the spline (see Remarks)
    - 说明: Obsolete. Superseded by ISketchManager::CreateSpline2.
- `ISketchManager.CreateSpline2(PointData: Object, SimulateNaturalEnds: bool)` → `SketchSegment`
    - PointData: Array of X,Y,Z point coordinates to use in creating the spline (see Remarks)
    - SimulateNaturalEnds: True to simulate natural ends, false to not simulate natural ends
    - 说明: Obsolete. Superseded by ISketchManager::CreateSpline3.
- `ISketchManager.CreateSpline3(PointData: Object, Surfs: Object, Direction: Object, SimulateNaturalEnds: bool, Status: Object)` → `Object`
    - PointData: Array of X,Y,Z coordinates of the spline points (see Remarks)
    - Surfs: Array of ISurface objects; null or Nothing for 2D splines (see Remarks)
    - Direction: Array of IMathVector objects; valid only for on-surface splines (see Remarks)
    - SimulateNaturalEnds: True to simulate natural ends, false to not; valid only for 2D splines (see Remarks)
    - Status: Array of boolean values; empty for 2D splines (see Remarks)
    - 说明: Creates either a 2D spline or a spline constrained to a surface.
- `ISketchManager.CreateCircularSketchStepAndRepeat(ArcRadius: double, ArcAngle: double, PatternNum: int, PatternSpacing: double, PatternRotate: bool, DeleteInstances: str, RadiusDim: bool, AngleDim: bool, CreateNumOfInstancesDim: bool)` → `bool`
    - ArcRadius: Radius for the circular sketch pattern
    - ArcAngle: Angle relative to the sketch entities being patterned
    - PatternNum: Total number of instances, including the seed geometry
    - PatternSpacing: Spacing between pattern instances
    - PatternRotate: True to rotate the pattern, false to not
    - DeleteInstances: Number of instances to delete, passed as a string formatted as: "(a) (b) (c) "
    - RadiusDim: True to display the radius dimension in the graphics area, false to not
    - AngleDim: True to display the angle dimension in the graphics area, false to not
    - CreateNumOfInstancesDim: True to display the number of instances dimension in the graphics area, false to not
    - 说明: Creates circular sketch pattern.
- `ISketchManager.CreateLinearSketchStepAndRepeat(NumX: int, NumY: int, SpacingX: double, SpacingY: double, AngleX: double, AngleY: double, DeleteInstances: str, XSpacingDim: bool, YSpacingDim: bool, AngleDim: bool, CreateNumOfInstancesDimInXDir: bool, CreateNumOfInstancesDimInYDir: bool)` → `bool`
    - NumX: Total number of instances along the x axis, including the seed
    - NumY: Total number of instances along the y axis, including the seed
    - SpacingX: Spacing between instances along the x axis
    - SpacingY: Spacing between instances along the y axis
    - AngleX: Angle for direction 1 relative to the x axis
    - AngleY: Angle for direction 2 relative to the y axis
    - DeleteInstances: Number of instances to delete, passed as a string in the format: "(a) (b) (c) "
    - XSpacingDim: True to display the spacing between instances dimension along the x axis in the graphics area, false to not
    - YSpacingDim: True to display the spacing between instances dimension along the y axis in the graphics area, false to not
    - AngleDim: True to display the angle dimension between axes in the graphics area, false to not
    - CreateNumOfInstancesDimInXDir: True to display the number of instances in the x direction dimension in the graphics area, false to not
    - CreateNumOfInstancesDimInYDir: True to display the number of instances in the y direction dimension in the graphics area, false to not
    - 说明: Creates a linear sketch pattern.
- `ISketchManager.AddToDB()` → `bool`
    - 说明: Gets or sets whether sketch entities are added directly to the SOLIDWORKS database.
- `ISketchManager.DisplayWhenAdded()` → `bool`
    - 说明: Gets or sets whether new sketch entities are immediately displayed when created.
- `ISketchManager.CreateFillet(Radius: double, ConstrainedCorners: int)` → `SketchSegment`
    - Radius: Radius of the fillet in meters
    - ConstrainedCorners: Action to take as defined in swConstrainedCornerAction_e (see Remarks)
    - 说明: Creates a sketch fillet using the selected sketch entities.
- `ISketchManager.SketchTrim(Option: int, X: double, Y: double, Z: double)` → `bool`
    - Option: Sketch trim option as defined in swSketchTrimChoice_e (see Remarks)
    - X: x coordinate of pick location (see Remarks)
    - Y: y coordinate of pick location (see Remarks)
    - Z: z coordinate of pick location (for 3D sketch) (see Remarks)
    - 说明: Trims the selected sketch entities.
- `ISketchManager.SketchOffset2(Offset: double, BothDirections: bool, Chain: bool, CapEnds: int, MakeConstruction: int, AddDimensions: bool)` → `bool`
    - Offset: Offset value; negative value offsets the sketch entities in the opposite direction
    - BothDirections: True to offset the sketch entities in both directions, false to offset the sketch entities in one direction
    - Chain: True to offset the chain of sketch entities, false to offset only the selected sketch entities (see Remarks)
    - CapEnds: Cap the ends as defined by swSkOffsetCapEndType_e
    - MakeConstruction: Convert original and offset sketch entities to construction sketch entities as defined by swSkOffsetMakeConstructionType_e
    - AddDimensions: True to include the offset distance in the sketch, false to not
    - 说明: Offsets the selected sketch entities.
- `ISketchManager.CreatePoint(X: double, Y: double, Z: double)` → `SketchPoint`
    - X: X location of the point
    - Y: Y location of the point
    - Z: Z location of the point; ignored for 2D sketches
    - 说明: Creates a sketch point in the active 2D or 3D sketch.
- `ISketchManager.CreateCenterLine(X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double)` → `SketchSegment`
    - X1: X coordinate of first end point, in meters
    - Y1: Y coordinate of first end point, in meters
    - Z1: Z coordinate of first end point, in meters
    - X2: X coordinate of second end point, in meters
    - Y2: Y coordinate of second end point, in meters
    - Z2: Z coordinate of second end point, in meters
    - 说明: Creates a center line between the specified points.
- `ISketchManager.CreateSketchSlot(SlotCreationType: int, SlotLengthType: int, Width: double, X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double, X3: double, Y3: double, Z3: double, CenterArcDirection: int, AddDimension: bool)` → `SketchSlot`
    - SlotCreationType: Type of sketch slot as defined in swSketchSlotCreationType_e
    - SlotLengthType: Type of length of sketch slot as defined in swSketchSlotLengthType_e
    - Width: Width of sketch slot
    - X1: x coordinate of point 1
    - Y1: y coordinate of point 1
    - Z1: z coordinate of point 2
    - X2: x coordinate of point 2
    - Y2: y coordinate of point 2
    - Z2: z coordinate of point 2
    - X3: x coordinate of point 3
    - Y3: y coordinate of point 3
    - Z3: z coordinate of point 3
    - CenterArcDirection: -1 = Clockwise (CW)
 1 = Counterclockwise (CCW)
    - AddDimension: True to automatically add dimensions, false to not
    - 说明: Creates a sketch slot.
- `ISketchManager.CreateCircleByRadius(XC: double, YC: double, Zc: double, Radius: double)` → `SketchSegment`
    - XC: X coordinate of the circle center point in meters
    - YC: Y coordinate of the circle center point in meters
    - Zc: Z coordinate of the circle center point in meters
    - Radius: Radius of the circle in meters
    - 说明: Creates a circle based on a center point and a specified radius.
- `ISketchManager.ActiveSketch()` → `Sketch`
    - 说明: Gets the active sketch.
- `ISketchManager.CreatePolygon(XC: double, YC: double, Zc: double, Xp: double, Yp: double, Zp: double, Sides: int, Inscribed: bool)` → `Object`
    - XC: X coordinate for the center
    - YC: Y coordinate for the center
    - Zc: Z coordinate for the center
    - Xp: X coordinate of a vertex
    - Yp: Y coordinate of a vertex
    - Zp: Z coordinate of a vertex
    - Sides: Number of sides in the polygon
    - Inscribed: True to show an inscribed construction circle, false to show a circumscribed construction circle
    - 说明: Creates a polygon in the active sketch.
- `ISketchManager.CreateParallelogram(X1: double, Y1: double, Z1: double, X2: double, Y2: double, Z2: double, X3: double, Y3: double, Z3: double)` → `Object`
    - X1: X coordinate for point 1
    - Y1: coordinate for point 1
    - Z1: Z coordinate for point 1
    - X2: X coordinate for point 2
    - Y2: Y coordinate for point 2
    - Z2: Z coordinate for point 2
    - X3: X coordinate for point 3
    - Y3: Y coordinate for point 3
    - Z3: Z coordinate for point 3
    - 说明: Creates a parallelogram.
- `ISketchManager.SketchUseEdge3(Chain: bool, InnerLoops: bool)` → `bool`
    - Chain: True to convert an entire chain of sketch entities, false to convert only the selected sketch entities (see Remarks)
    - InnerLoops: True to convert the inner loops of the selected faces to sketch entities, false to not
    - 说明: Creates sketch entities on a sketch plane by projecting selected edges, loops, faces, curves, and external sketch contours.
- `ISketchManager.CreateSplinesByEqnParams(Parameters: Object)` → `Object`
    - Parameters: Array containing an array of doubles to use in creating the spline (see Remarks)
    - 说明: Obsolete. Superseded by ISketchManager::CreateSplinesByEqnParams2.

## IFeatureManager

- `IFeatureManager.FeatureExtrusion3(Sd: bool, Flip: bool, Dir: bool, T1: int, T2: int, D1: double, D2: double, Dchk1: bool, Dchk2: bool, Ddir1: bool, Ddir2: bool, Dang1: double, Dang2: double, OffsetReverse1: bool, OffsetReverse2: bool, TranslateSurface1: bool, TranslateSurface2: bool, Merge: bool, UseFeatScope: bool, UseAutoSelect: bool, T0: int, StartOffset: double, FlipStartOffset: bool)` → `Feature`
    - Sd: True for single ended, false for double ended
    - Flip: True to flip the side to cut
    - Dir: True to flip the direction of extrusion
    - T1: Termination type for first end of the extrusion as defined in swEndConditions_e
    - T2: Termination type for second end of the extrusion as defined in swEndConditions_e
    - D1: Depth of extrusion for first end in meters; offset, if T1 is set to swEndConditions_e.swEndCondOffsetFromSurface
    - D2: Depth of extrusion for second end in meters; offset, if T2 is set to swEndConditions_e.swEndCondOffsetFromSurface
    - Dchk1: True to allow drafting in the first direction, false to not
    - Dchk2: True to allow drafting in the second direction, false to not
    - Ddir1: True for first draft angle to be inward, false to be outward; valid only if Dchk1 is true
    - Ddir2: True for second draft angle to be inward, false to be outward; valid only if Dchk2 is true
    - Dang1: Draft angle for first end; valid only if Dchk1 is true
    - Dang2: Draft angle for second end; valid only if Dchk2 is true
    - OffsetReverse1: True to offset the first end from another face or plane in a direction away from the sketch, false to offset in a direction toward the sketch; valid o
    - OffsetReverse2: True to offset the second end from another face or plane in a direction away from the sketch, false to offset in a direction toward the sketch; valid 
    - TranslateSurface1: True if the first end of the extrusion is a translation of the reference surface, false if it has a true offset; valid only if T1 is set to swEndCondi
    - TranslateSurface2: True if the second end of the extrusion is a translation of the reference surface, false if it has a true offset; valid only if T2 is set to swEndCond
    - Merge: True to merge the results in a multibody part, false to not
    - UseFeatScope: True if the feature only affects selected bodies, false if the feature affects all bodies (see Remarks)
    - UseAutoSelect: True to automatically select all bodies and have the feature affect those bodies, false to select the bodies that the feature affects (see Remarks)
    - T0: Start condition as defined in swStartConditions_e
    - StartOffset: Distance from the sketch plane to start the extrude; valid only if T0 is set to swStartConditions_e.swStartOffset
    - FlipStartOffset: True to flip the direction of the start offset, false to not; valid only if T0 is set to swStartConditions_e.swStartOffset
    - 说明: Creates an extruded feature.
- `IFeatureManager.FeatureCut4(Sd: bool, Flip: bool, Dir: bool, T1: int, T2: int, D1: double, D2: double, Dchk1: bool, Dchk2: bool, Ddir1: bool, Ddir2: bool, Dang1: double, Dang2: double, OffsetReverse1: bool, OffsetReverse2: bool, TranslateSurface1: bool, TranslateSurface2: bool, NormalCut: bool, UseFeatScope: bool, UseAutoSelect: bool, AssemblyFeatureScope: bool, AutoSelectComponents: bool, PropagateFeatureToParts: bool, T0: int, StartOffset: double, FlipStartOffset: bool, OptimizeGeometry: bool)` → `Feature`
    - Sd: True for a single-ended cut, false for a doubled-ended cut
    - Flip: True to remove material outside of the profile of the flip side to cut, false to not
    - Dir: True for Direction 1 to be opposite of the default direction (see Remarks)
    - T1: Termination type for the first end as defined in swEndConditions_e
    - T2: Termination type for the second end as defined in swEndConditions_e
    - D1: Depth of extrusion for the first end in meters
    - D2: Depth of extrusion for the second end in meters
    - Dchk1: True allows a draft angle in the first direction, false does not allow drafting in the first direction
    - Dchk2: True allows a draft angle in the second direction, false does not allow drafting in the second direction
    - Ddir1: True for the first draft angle to be inward, false to be outward; only valid when Dchk1 is true
    - Ddir2: True for the second draft angle to be inward, false to be outward; only valid when Dchk2 is true
    - Dang1: Draft angle for the first end; only valid when Dchk1 is true
    - Dang2: Draft angle for the second end; only valid when Dchk2 is true
    - OffsetReverse1: If you chose to offset the first end condition from another face or plane, then true specifies offset in direction away from the sketch, false specifi
    - OffsetReverse2: If you chose to offset the second end condition from another face or plane, then true specifies offset in direction away from the sketch, false specif
    - TranslateSurface1: When you choose swEndConditions_e.swEndCondOffsetFromSurface as the termination type for the first end, then true specifies that the end of the extrus
    - TranslateSurface2: When you choose swEndConditions_e.swEndCondOffsetFromSurface as the termination type for the second end, then true specifies that the end of the extru
    - NormalCut: True to create the cut normal to the sheet metal thickness, false to not (only valid for sheet metal parts; use false for non-sheet metal parts)
    - UseFeatScope: True if the feature only affects selected bodies or components, false if the feature affects all bodies or components
    - UseAutoSelect: True to automatically select all bodies or components and have the feature affect those bodies or components, false to only select the bodies or compo
    - AssemblyFeatureScope: True if the assembly feature only affects selected components in the assembly, false if the assembly feature affects all components in the assembly
    - AutoSelectComponents: True to automatically select all affected components, false to use only the selected components
    - PropagateFeatureToParts: True to propagate the assembly feature to the components in the model that it affects, false to not
    - T0: Start conditions as defined in swStartConditions_e
    - StartOffset: If T0 is swStartConditions_e.swStartOffset, then specify an offset value
    - FlipStartOffset: If T0 is swStartConditions_e.swStartOffset, then true to flip the direction of cut, false to not
    - OptimizeGeometry: True to optimize the normal cut in a sheet metal part, false to not; only valid for sheet metal parts and when NormalCut is true
    - 说明: Creates a cut extrude feature.
- `IFeatureManager.FeatureRevolve2(SingleDir: bool, IsSolid: bool, IsThin: bool, IsCut: bool, ReverseDir: bool, BothDirectionUpToSameEntity: bool, Dir1Type: int, Dir2Type: int, Dir1Angle: double, Dir2Angle: double, OffsetReverse1: bool, OffsetReverse2: bool, OffsetDistance1: double, OffsetDistance2: double, ThinType: int, ThinThickness1: double, ThinThickness2: double, Merge: bool, UseFeatScope: bool, UseAutoSelect: bool)` → `Feature`
    - SingleDir: True if the revolve is in one direction, false if in two directions (see Remarks)
    - IsSolid: True if this is a solid revolve feature, false if not
    - IsThin: True if this is a thin revolve feature, false if not
    - IsCut: True if this is a cut revolve feature, false if not
    - ReverseDir: True reverses the angle of the revolution, false does not; only applies if Dir1Type is not swEndConditions_e.swEndCondMidPlane
    - BothDirectionUpToSameEntity: True if the revolve is up to the same entity in both directions, false if not; only applies if SingleDir is false and Dir1Type and Dir2Type are swEndC
    - Dir1Type: Revolve end condition as defined in swEndConditions_e
    - Dir2Type: Revolve end condition in direction 2; as defined in swEndConditions_e and only applies if Dir1Type is not swEndConditions_e.swEndCondMidPlane
    - Dir1Angle: Angle in radians of revolution in direction 1; only applies if Dir1Type is swEndConditions_e.swEndCondBlind
    - Dir2Angle: Angle in radians of revolution in direction 2; only applies if Dir2Type is swEndConditions_e.swEndCondBlind
    - OffsetReverse1: True to reverse the offset direction in direction 1, false to not; only applies if Dir1Type is swEndConditions_e.swEndCondOffsetFromSurface
    - OffsetReverse2: True to reverse the offset direction in direction 2, false to not; only applies if Dir2Type is swEndConditions_e.swEndCondOffsetFromSurface
    - OffsetDistance1: Offset distance in direction 1; only applies if Dir1Type is swEndConditions_e.swEndCondOffsetFromSurface
    - OffsetDistance2: Offset distance in direction 2; only applies if Dir2Type is swEndConditions_e.swEndCondOffsetFromSurface
    - ThinType: Type and direction as defined in swThinWallType_e
    - ThinThickness1: Wall thickness in direction 1 (if ThinType is swThinWallType_e.swThinWallMidPlane, (ThinThickness1)/2 is used for each direction)
    - ThinThickness2: Wall thickness in direction 2 (only applies if ThinType is swThinWallType_e.swThinWallTwoDirection)
    - Merge: True to merge the results into a multi-body part, false to not
    - UseFeatScope: True if the feature only affects selected bodies, false if the feature affects all bodies (see Remarks)
    - UseAutoSelect: True to automatically select all bodies and have the feature affect those bodies, false to select the bodies or components that the feature affects (s
    - 说明: Creates a base-, boss-, or cut-revolve feature.
- `IFeatureManager.FeatureRevolveCut2(Angle: double, ReverseDir: bool, Angle2: double, RevType: int, Options: int, UseFeatScope: bool, UseAutoSelect: bool, AssemblyFeatureScope: bool, AutoSelectComponents: bool, PropagateFeatureToParts: bool)` → `Feature`
    - Angle: Angle of revolution in radians
    - ReverseDir: True reverses the angle direction, false does not
    - Angle2: Angle of revolution in radians
    - RevType: Type of revolution as defined in swRevolveType_e (can be either mid-plane or two-direction)
    - Options: Additional control (see Remarks)
    - UseFeatScope: True if the feature only affects selected bodies, false if the feature affects all bodies
    - UseAutoSelect: True to automatically select all bodies and have the feature affect those bodies, false to select the bodies the feature affects (see Remarks)
    - AssemblyFeatureScope: True if the assembly feature only affects selected components in the assembly, false if the assembly feature affects all components in the assembly
    - AutoSelectComponents: True to auto-select all affected components, false to not (use the selected components)
    - PropagateFeatureToParts: True to propagate the assembly feature to the components, false to not
    - 说明: Obsolete. Superseded by IFeatureManager::FeatureRevolve2.
- `IFeatureManager.FeatureFillet3(Options: int, R1: double, R2: double, Rho: double, Ftyp: int, OverflowType: int, ConicRhoType: int, Radii: Object, Dist2Arr: Object, RhoArr: Object, SetBackDistances: Object, PointRadiusArray: Object, PointDist2Array: Object, PointRhoArray: Object)` → `Object`
    - Options: Feature fillet options as defined in swFeatureFilletOptions_e (see Remarks)
    - R1: Uniform radius of the symmetric fillet; valid only if:
 
 Ftyp != swFeatureFilletType_e.swFeatureFilletType_VariableRadius
 Options include swFeatureF
    - R2: Distance 2 radius of the asymmetric fillet; valid only if:
 
 Ftyp != swFeatureFilletType_e.swFeatureFilletType_VariableRadius
 Options include swFeat
    - Rho: Value that determines the conic rho or radius of the fillet:
 
 Ftyp != swFeatureFilletType_e.swFeatureFilletType_VariableRadius
 Conic rho value [0.0
    - Ftyp: Type of fillet as defined in swFeatureFilletType_e (see Remarks)
    - OverflowType: Control of fillet overflowing onto adjacent surfaces as defined in swFilletOverFlowType_e (see Remarks)
    - ConicRhoType: Fillet cross-section profile as defined in swFeatureFilletProfileType_e; valid only if Options does not include swFeatureFilletOptions_e.swFeatureFill
    - Radii: Array containing the radii for selected edges for the symmetric variable radius fillet; valid only if:
 
 Ftyp = swFeatureFilletType_e.swFeatureFillet
    - Dist2Arr: Array containing the Distance 2 radii for selected edges for the asymmetric variable radius fillet; valid only if:
 
 Ftyp = swFeatureFilletType_e.swF
    - RhoArr: Array of conic rho or radius values for the specified ConicRhoType for selected edges for the variable radius fillet; valid only if:
 
 Conic rho valu
    - SetBackDistances: Array assigning setback distances on edges meeting at a selected fillet corner (see Remarks)
    - PointRadiusArray: Array containing control point radii along the lengths of the selected edges for the symmetric variable radius fillet; valid only if:
 
 Ftyp = swFeat
    - PointDist2Array: Array containing Distance 2 control point radii along the lengths of the selected edges for the asymmetric variable radius fillet; valid only if:
 
 F
    - PointRhoArray: Array of conic rho or radius values for the specified ConicRhoType at various control points along the lengths of the selected edges for the variable 
    - 说明: Creates a fillet feature for selected edges and control point references.
- `IFeatureManager.InsertFeatureChamfer(Options: int, ChamferType: int, Width: double, Angle: double, OtherDist: double, VertexChamDist1: double, VertexChamDist2: double, VertexChamDist3: double)` → `Feature`
    - Options: Options as defined by swFeatureChamferOption_e
    - ChamferType: Chamfer type as defined by swChamferType_e
    - Width: If ChamferType is swChamferAngleDistance, then specify width of chamfer
    - Angle: If ChamferType is swChamferAngleDistance, then specify the angle of the }}-->}}-->chamfer
    - OtherDist: If ChamferType is swChamferEqualDistance, then you can specify a single value so that all distances are equal
    - VertexChamDist1: If ChamferType is swChamferDistanceDistance or swChamferVertex, then specify a value for the distance on first side
    - VertexChamDist2: If ChamferType is swChamferDistanceDistance or swChamferVertex, then specify a value for the distance on second side
    - VertexChamDist3: If ChamferType is swChamferVertex, then specify a value for the distance on third side
    - 说明: Inserts a chamfer.
- `IFeatureManager.FeatureCircularPattern5(Number: int, Spacing: double, FlipDirection: bool, DName: str, GeometryPattern: bool, EqualSpacing: bool, VaryInstance: bool, SyncSubAssemblies: bool, BDir2: bool, BSymmetric: bool, Number2: int, Spacing2: double, DName2: str, EqualSpacing2: bool)` → `Feature`
    - Number: Number of instances of the circular pattern to insert in Direction 1, including the original instance
    - Spacing: Spacing between each instance in Direction 1 of the circular pattern or, if EqualSpacing is true, then the total angle in radians
    - FlipDirection: True to flip the direction of the circular pattern in Direction 1, false to not
    - DName: Name of the angular dimension defining Direction 1 of the pattern
    - GeometryPattern: True to use geometry pattern, false to not
    - EqualSpacing: True to use equal spacing in Direction 1, false to not
    - VaryInstance: True to vary the dimensions or spacing of individual pattern instances, false to not; valid only if GeometryPattern = false (see Remarks)
    - SyncSubAssemblies: True to move components in the patterned instances when components are moved in the seed flexible subassembly, false to not
    - BDir2: True to create a bidirectional circular pattern feature, false to not
    - BSymmetric: True to create a symmetric circular pattern feature in Direction 2, false to create an asymmetrical circular pattern feature in Direction 2; valid onl
    - Number2: Number of instances to insert in Direction 2; valid only if BDir2 is true
    - Spacing2: Distance between pattern instances in Direction 2; valid only if BDir2 is true
    - DName2: Name of the angular dimension defining Direction 2 of the pattern; valid only if BDir2 is true
    - EqualSpacing2: True to use equal spacing in Direction 2, false to not; valid only if BDir2 is true and BSymmetric is false
    - 说明: Obsolete. See IFeatureManager::CreateFeature and the Remarks in ICircularPatternFeatureData and ILocalCircularPatternFeatureData.
- `IFeatureManager.FeatureLinearPattern5(Num1: int, Spacing1: double, Num2: int, Spacing2: double, FlipDir1: bool, FlipDir2: bool, DName1: str, DName2: str, GeometryPattern: bool, VaryInstance: bool, HasOffset1: bool, HasOffset2: bool, CtrlByNum1: bool, CtrlByNum2: bool, FromCentroid1: bool, FromCentroid2: bool, RevOffset1: bool, RevOffset2: bool, Offset1: double, Offset2: double, D2PatternSeedOnly: bool, SyncSubAssemblies: bool)` → `Feature`
    - Num1: Number of instances of the linear pattern in Direction 1, including the original
    - Spacing1: Spacing in meters between each instance of the linear pattern in Direction 1
    - Num2: Number of instances of the linear pattern in Direction 2, including the original
    - Spacing2: Spacing in meters between each instance of the linear pattern in Direction 2
    - FlipDir1: True to reverse the direction of the linear pattern in Direction 1, false to not
    - FlipDir2: True to reverse the direction of the linear pattern in Direction 2, false to not
    - DName1: Name of the dimension defining Direction 1
    - DName2: Name of the dimension defining Direction 2
    - GeometryPattern: True to use geometry pattern, false to not
    - VaryInstance: True to vary the dimensions and spacing of individual pattern instances, false to not; valid only if GeometryPattern = false (see Remarks)
    - HasOffset1: True if using Offset1 to specify an offset of the pattern seed from a selected reference in Direction 1, false if not
    - HasOffset2: True if using Offset2 to specify an offset of the pattern seed from a selected reference in Direction 2, false if not
    - CtrlByNum1: True to control pattern spacing using Num1, false to control it using Spacing1; valid only if HasOffset1 is true
    - CtrlByNum2: True to control pattern spacing using Num2, false to control it using Spacing2; valid only if HasOffset2 is true
    - FromCentroid1: True if Offset1 is measured from the centroid of the seed instance, false if it is measured from a selected reference on the seed instance; valid only
    - FromCentroid2: True if Offset2 is measured from the centroid of the seed instance, false if it is measured from a selected reference on the seed instance; valid only
    - RevOffset1: True to reverse the direction of Offset1, false to not; valid only if HasOffset1 is true
    - RevOffset2: True to reverse the direction of Offset2, false to not; valid only if HasOffset2 is true
    - Offset1: Offset in meters from a selected reference in Direction 1; valid only if HasOffset1 is true (see Remarks)
    - Offset2: Offset in meters from a selected reference in Direction 2; valid only if HasOffset2 is true (see Remarks)
    - D2PatternSeedOnly: True to create a linear pattern in Direction 2 using the seed features only, without replicating the pattern instances of Direction 1; false to not
    - SyncSubAssemblies: True to move components in the patterned instances when components are moved in the seed flexible subassembly, false to not
    - 说明: Obsolete. See IFeatureManager::CreateFeature and the Remarks in ILinearPatternFeatureData and ILocalLinearPatternFeatureData.
- `IFeatureManager.InsertMirrorFeature2(BMirrorBody: bool, BGeometryPattern: bool, BMerge: bool, BKnit: bool, ScopeOptions: int)` → `Feature`
    - BMirrorBody: True to mirror solid bodies; false to mirror a feature or face
    - BGeometryPattern: True to mirror only the feature geometry, false to solve the entire feature; applies to mirroring features only
    - BMerge: True to merge any mirrored solid bodies, false to not; applies to mirroring solid bodies only
    - BKnit: True to knit surfaces, false to not; applies to mirroring surfaces only
    - ScopeOptions: Feature scope as defined by swFeatureScope_e
    - 说明: Mirrors selected features, faces, bodies, and structure systems about a selected plane or planar face.
- `IFeatureManager.InsertProtrusionBlend2(Closed: bool, KeepTangency: bool, ForceNonRational: bool, TessToleranceFactor: double, StartMatchingType: Short, EndMatchingType: Short, StartTangentLength: double, EndTangentLength: double, StartTangentDir: bool, EndTangentDir: bool, IsThinBody: bool, Thickness1: double, Thickness2: double, ThinType: Short, Merge: bool, UseFeatScope: bool, UseAutoSelect: bool, GuideCurveInfluence: int)` → `Feature`
    - Closed: True closes the loft, false leaves the loft open; if true and less than three profiles are selected, then any selected guide curves must be closed cur
    - KeepTangency: True maintains the tangency as seen in the section curves, false does not; if the section curves are tangent, then you have the option to specify whet
    - ForceNonRational: True obtains smoother surfaces, false does not
    - TessToleranceFactor: Factor that controls the number of intermediate sections used for loft with centerline; the default value is 1.0; the greater the value, the more inte
    - StartMatchingType: Tangency type at the start profile:
 
 0 = none
 
 1 = tangent to the normal of the profile
 
 2 = tangent to a selected vector
 
 3 = tangency to all
    - EndMatchingType: Tangency type at the end profile:
 
 0 = none
 
 1 = tangent to the normal of the profile
 
 2 = tangent to a selected vector
 
 3 = tangency to all t
    - StartTangentLength: Start tangent length
    - EndTangentLength: End tangent length
    - StartTangentDir: True is one direction, false is the opposite
    - EndTangentDir: True is one direction, false is the opposite
    - IsThinBody: True if this feature is a thin body, false if it is not
    - Thickness1: Thickness value for the first direction
    - Thickness2: Thickness value for the second direction
    - ThinType: Thin wall type:
 
 0 = One direction
 
 1 = One direction reverse
 
 2 = Mid-plane
 
 3 = Two direction
    - Merge: True merges the results in a multibody part, false does not
    - UseFeatScope: True if the feature only affects selected bodies, false if the feature affects all bodies
    - UseAutoSelect: True to automatically select all bodies and have the feature affect those bodies, false to select the bodies the feature affects
    - GuideCurveInfluence: Guide curves influence as defined in swGuideCurveInfluence_e
    - 说明: Creates a lofted body or boss from the selected profiles, centerline, and guide curves.
- `IFeatureManager.FeatureSketchDrivenPattern(UseCentroid: bool, BGeomPatt: bool)` → `Feature`
    - UseCentroid: True to use a centroid as the reference point, false to not
    - BGeomPatt: True to pattern using only the feature geometry, false to pattern each instance
    - 说明: Obsolete. See IFeatureManager::CreateFeature and the Remarks in ISketchPatternFeatureData.
- `IFeatureManager.InsertSheetMetalBaseFlange2(Thickness: double, ThickenDir: bool, Radius: double, ExtrudeDist1: double, ExtrudeDist2: double, FlipExtruDir: bool, EndCondition1: int, EndCondition2: int, DirToUse: int, PCBA: CustomBendAllowance, UseDefaultRelief: bool, ReliefType: int, ReliefWidth: double, ReliefDepth: double, ReliefRatio: double, UseReliefRatio: bool, Merge: bool, UseFeatScope: bool, UseAutoSelect: bool)` → `Feature`
    - Thickness: Wall thickness of the sheet metal feature
    - ThickenDir: Direction to thicken the sheet metal
    - Radius: Global bend radius to insert at the corners
    - ExtrudeDist1: Distance of the sheet metal extrusion for the Direction 1
    - ExtrudeDist2: Distance of the sheet metal extrusion for the Direction 2
    - FlipExtruDir: True to reverse the direction of the extrude, false to not
    - EndCondition1: End condition for first extrude distance
    - EndCondition2: End condition for second extrude distance
    - DirToUse: End condition type as defined in swEndConditions_e
    - PCBA: Pointer to the custom bend allowance object
    - UseDefaultRelief: True to use the default relief, false to not
    - ReliefType: Relief type as defined in swSheetMetalReliefTypes_e
    - ReliefWidth: Width of the relief
    - ReliefDepth: Depth of the relief
    - ReliefRatio: Value for the relief ratio
    - UseReliefRatio: True to use the relief ratio, false to not
    - Merge: True to connect bodies into a single sheet metal body; false to not
    - UseFeatScope: True to use selected bodies, false to use all bodies
    - UseAutoSelect: True to auto select bodies (see Remarks)
    - 说明: Obsolete. Superseded by IFeatureManager::CreateDefinition and IFeatureManager::CreateFeature.

## IDimension

- `IDimension.SetSystemValue3(NewValue: double, WhichConfigurations: int, Config_names: Object)` → `int`
    - NewValue: Dimension value in meters
    - WhichConfigurations: Configuration in which to set this value as defined in swSetValueInConfiguration_e (see Remarks)
    - Config_names: Names of the configurations (see Remarks)
    - 说明: Sets the value of this dimension in system units (meters) in the specified configuration.
- `IDimension.GetSystemValue3(WhichConfigurations: int, Config_names: Object)` → `Object`
    - WhichConfigurations: Configurations to get this value from as defined by swInConfigurationOpts_e (see Remarks)
    - Config_names: Names of the configuration (see Remarks)
    - 说明: Gets the value of the current dimension in system units in the named configuration.
- `IDimension.FullName()` → `str`
    - 说明: Gets the full name of a dimension including the feature and the model.
- `IDimension.Value()` → `double`
    - 说明: Obsolete. Superseded by IDimension::GetValue3, IDimension::IGetValue3, IDimension::ISetValue3, and IDimension::SetValue3.
- `IDimension.SetValue2(NewValue: double, WhichConfigurations: int)` → `int`
    - 说明: Obsolete. Superseded by IDimension::SetValue3.
- `IDimension.SystemValue()` → `double`
    - 说明: Obsolete. Superseded by IDimension::GetSystemValue3, IDimension::IGetSystemValue3, IDimension::SetSystemValue3, and IDimension::ISetSystemValue3.
- `IDimension.Name()` → `str`
    - 说明: Gets or sets the name of a dimension.
- `IDimension.GetNameForSelection()` → `str`
    - 说明: Gets the name of the selected dimension needed by IModelDocExtension::SelectByID2.

## ICustomPropertyManager

- `ICustomPropertyManager.Add3(FieldName: str, FieldType: int, FieldValue: str, OverwriteExisting: int)` → `int`
    - FieldName: Name of custom property
    - FieldType: Type of custom property as defined in swCustomInfoType_e
    - FieldValue: Value of custom property
    - OverwriteExisting: Overwrite option as defined in swCustomPropertyAddOption_e
    - 说明: Adds a custom property to a configuration or model document.
- `ICustomPropertyManager.Get5(FieldName: str, UseCached: bool, ValOut: str, ResolvedValOut: str, WasResolved: bool)` → `int`
    - FieldName: Name of the custom property
    - UseCached: True if the configuration has been activated, false if not (see Remarks)
    - ValOut: Value of the custom property
    - ResolvedValOut: Evaluated value of the custom property
    - WasResolved: True if the value was evaluated, false if not (see Remarks)
    - 说明: Obsolete. Superseded by ICustomPropertyManager::Get6.
- `ICustomPropertyManager.Set2(FieldName: str, FieldValue: str)` → `int`
    - FieldName: Name of the existing custom property
    - FieldValue: Value for the existing custom property
    - 说明: Sets the value for the specified custom property.
- `ICustomPropertyManager.GetAll3(PropNames: Object, PropTypes: Object, PropValues: Object, Resolved: Object, PropLink: Object)` → `int`
    - PropNames: Array of the names of custom properties retrieved
    - PropTypes: Array of types of PropNames as defined in swCustomInfoType_e
    - PropValues: Array of evaluated values of PropNames
    - Resolved: Array of evaluation statuses of PropNames as defined in swCustomInfoGetResult_e
    - PropLink: Array of integers indicating whether PropNames are linked to their parent parts:
 1 = link
 0 = no link
    - 说明: Gets all of the custom properties for this configuration.
- `ICustomPropertyManager.Delete2(FieldName: str)` → `int`
    - FieldName: Name of the custom property to delete
    - 说明: Deletes the specified custom property.

## IEquationMgr

- `IEquationMgr.Add3(Index: int, Equation: str, Solve: bool, WhichConfigurations: int, ConfigNames: Object)` → `int`
    - Index: 0-based index of the new equation (-1 places it at the end of the list)
    - Equation: String containing the equation (see Remarks)
    - Solve: True to solve the equation immediately; false to solve later (see Remarks)
    - WhichConfigurations: Configuration option as defined in swInConfigurationOpts_e (see Remarks)
    - ConfigNames: Array of the names of the configurations to which to add this equation; valid only if WhichConfigurations is set to swInConfigurationOpts_e.swSpecifyC
    - 说明: Adds an equation at the specified index for the specified configurations.
- `IEquationMgr.SetEquationAndConfigurationOption(Index: int, Equation: str, WhichConfigurations: int, ConfigNames: Object)` → `int`
    - Index: 0-based index of the equation to modify
    - Equation: String containing the modified equation (see Remarks)
    - WhichConfigurations: Configuration option as defined in swInConfigurationOpts_e
    - ConfigNames: Array of the names of the configurations to which to add this equation; valid only if WhichConfigurations is set to swInConfigurationOpts_e.swSpecifyC
    - 说明: Modifies the equation at the specified index for the specified configurations.
- `IEquationMgr.Equation()` → `?`
    - 说明: Gets or sets the equation at the specified index.
- `IEquationMgr.GlobalVariable()` → `?`
    - 说明: Gets whether the equation at the specified index is a global variable.
- `IEquationMgr.GetCount()` → `int`
    - 说明: Gets the number of equations in the model.
- `IEquationMgr.Value()` → `?`
    - 说明: Gets the value of the equation at the specified index.

## IPartDoc

- `IPartDoc.GetPartBox(NoConversion: bool)` → `Object`
    - NoConversion: Convert to user units or not; true retains system units, false changes to user units
    - 说明: Gets the box enclosing the part.
- `IPartDoc.GetBodies2(BodyType: int, BVisibleOnly: bool)` → `Object`
    - BodyType: Type of body as defined in swBodyType_e
    - BVisibleOnly: True gets only the visible bodies, false gets all of the bodies in the part
    - 说明: Gets the bodies in this part.
- `IPartDoc.MaterialIdName()` → `str`
    - 说明: Gets or sets the material name.
- `IPartDoc.SetMaterialPropertyName2()` → `?`
    - 说明: Sets the name of the material property for the specified configuration.

## IBody2

- `IBody2.GetMassProperties(Density: double)` → `Object`
    - Density: Density to use for the mass property calculations on this body
    - 说明: Gets the mass properties of this body.
- `IBody2.GetBodyBox()` → `Object`
    - 说明: Gets the bounding box for this body.
- `IBody2.Name()` → `str`
    - 说明: Gets or sets the name of the selected body.
- `IBody2.GetFaces()` → `Object`
    - 说明: Gets all of the faces on the body.
- `IBody2.GetFirstFace()` → `Object`
    - 说明: Finds the first face in a body and returns the face.

## IMassProperty2

- `IMassProperty2.Mass()` → `double`
    - 说明: Gets the mass of selected components/bodies.
- `IMassProperty2.Volume()` → `double`
    - 说明: Gets the volume of selected components/bodies.
- `IMassProperty2.CenterOfMass()` → `Object`
    - 说明: Gets the center of mass of selected components/bodies.
- `IMassProperty2.Density()` → `double`
    - 说明: Gets the density of selected components/bodies.
- `IMassProperty2.SurfaceArea()` → `double`
    - 说明: Gets the surface area of selected components/bodies.
- `IMassProperty2.GetMomentOfInertia(WhereTaken: int)` → `Object`
    - WhereTaken: Coordinate system as defined in swMassPropertyMoment_e
    - 说明: Gets the moment of inertia at the specified coordinate system for the selected bodies/components.

## IUserUnit

- `IUserUnit.ConvertToSystemValue(UnitText: str, ComputedValue: double)` → `bool`
    - UnitText: Value to convert
    - ComputedValue: Converted value in document units
    - 说明: Converts a text string to a document unit value.

## IRefPlane

- `IRefPlane.GetRefPlaneParams()` → `Object`
    - 说明: Obsolete. Superseded by IRefPlane::Transform.

## IFeature

- `IFeature.Name()` → `str`
    - 说明: Gets or sets the name of the current feature.
- `IFeature.GetTypeName2()` → `str`
    - 说明: Gets the type of feature.
- `IFeature.GetTypeName()` → `str`
    - 说明: Gets the type of feature.
- `IFeature.Select2(Append: bool, Mark: int)` → `bool`
    - Append: True appends the feature to the current selection list, false replaces the current selection list
    - Mark: Value you want to use as a mark; this number is used by functions that require ordered selection
    - 说明: Selects and marks this feature.
- `IFeature.GetFirstSubFeature()` → `Object`
    - 说明: Gets the first sub-feature that belongs to this feature.
- `IFeature.GetNextFeature()` → `Object`
    - 说明: Gets the next feature in the part.
- `IFeature.GetID()` → `int`
    - 说明: Gets the feature ID of this feature.
- `IFeature.IsSuppressed()` → `bool`
    - 说明: Obsolete. Superseded by IFeature::IsSuppressed2.
- `IFeature.SetSuppression2(SuppressionState: int, Config_opt: int, Config_names: Object)` → `bool`
    - SuppressionState: Suppression state as defined in swFeatureSuppressionAction_e
    - Config_opt: Configuration option as defined in swInConfigurationOpts_e (see Remarks)
    - Config_names: Array of configuration names; valid only if Config_opt set to swInConfigurationOpts_e.swSpecifyConfiguration
    - 说明: Sets the suppression state of this feature.
