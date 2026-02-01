from commands import SayHelloCommand, LoginCommand, CreateUserCommand, LogoutCommand, \
    RefreshTokenCommand, MeCommand, UploadUserAvatarCommand, GenerateSecureDownloadLinkCommand, AddRoleCommand, \
    AssignRoleToUserCommand, GetCommandNamesCommand, SyncPermissionCommand, TransformTemplateRenderCommand, \
    RegisterGithubUserCommand, GithubSignInCommand, GithubStartCommand, CreateCompContentsWithUploadsCommand, \
    UpdateComponentWithUploadsCommand, DeleteCompContentCommand, ContentGetCommand, TagDeleteCommand, \
    TagListActiveCommand, TagCreateCommand, CreateCompPagesWithUploadsCommand, CreateCompLayoutsWithUploadsCommand, \
    UpdateLayoutWithUploadsCommand, DeleteCompLayoutCommand, LayoutGetCommand, CreateCompPagesWithUploadsCommand, \
    UpdatePageWithUploadsCommand, DeleteCompPageCommand, PageGetCommand, CreateCompAuthenticationsWithUploadsCommand, \
    UpdateCompAuthenticationsWithUploadsCommand, DeleteCompAuthenticationsCommand, AuthenticationGetCommand, \
    CreateCompNavigationsWithUploadsCommand, UpdateNavigationsWithUploadsCommand, DeleteCompNavigationCommand, \
    NavigationGetCommand, CreateFunnelWithUploadsCommand, UpdateFunnelWithUploadsCommand, DeleteFunnelsCommand, \
    FunnelGetCommand, CreateProjectWithUploadsCommand, UpdateProjectWithUploadsCommand, ProjectGetCommand, \
    DeleteProjectsCommand, CreateCompConnectionsCommand, DeleteCompConnectionsCommand, ConnectionsGetCommand, \
    CompConnectionPreview

command_registry = [
    CreateUserCommand(),
    LoginCommand(),
    SayHelloCommand(),
    RefreshTokenCommand(),
    LogoutCommand(),
    MeCommand(),
    UploadUserAvatarCommand(),
    GenerateSecureDownloadLinkCommand(),
    AddRoleCommand(),
    AssignRoleToUserCommand(),
    GetCommandNamesCommand(),
    SyncPermissionCommand(),
    TransformTemplateRenderCommand(),
    RegisterGithubUserCommand(),
    GithubSignInCommand(),
    GithubStartCommand(),
    CreateCompContentsWithUploadsCommand(),
    UpdateComponentWithUploadsCommand(),
    DeleteCompContentCommand(),
    ContentGetCommand(),
    TagListActiveCommand(),
    TagDeleteCommand(),
    TagCreateCommand(),
    CreateCompLayoutsWithUploadsCommand(),
    UpdateLayoutWithUploadsCommand(),
    DeleteCompLayoutCommand(),
    LayoutGetCommand(),
    CreateCompPagesWithUploadsCommand(),
    UpdatePageWithUploadsCommand(),
    DeleteCompPageCommand(),
    PageGetCommand(),
    CreateCompAuthenticationsWithUploadsCommand(),
    UpdateCompAuthenticationsWithUploadsCommand(),
    DeleteCompAuthenticationsCommand(),
    AuthenticationGetCommand(),
    CreateCompNavigationsWithUploadsCommand(),
    UpdateNavigationsWithUploadsCommand(),
    DeleteCompNavigationCommand(),
    NavigationGetCommand(),
    CreateFunnelWithUploadsCommand(),
    UpdateFunnelWithUploadsCommand(),
    DeleteFunnelsCommand(),
    FunnelGetCommand(),
    CreateProjectWithUploadsCommand(),
    UpdateProjectWithUploadsCommand(),
    ProjectGetCommand(),
    DeleteProjectsCommand(),
    CreateCompConnectionsCommand(),
    DeleteCompConnectionsCommand(),
    ConnectionsGetCommand(),
    CompConnectionPreview()
]
